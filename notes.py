#!/usr/bin/env python3
"""Lecture audio -> transcript + study notes. Runs locally on Apple Silicon.

    ./notes.py lecture.m4a

Transcription is local (mlx-whisper, free). Notes use the Claude API.
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path

WHISPER_REPOS = {
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "turbo": "mlx-community/whisper-large-v3-turbo",
}

# Primes Whisper toward code-switched speech instead of snapping to one language.
HINGLISH_PROMPT = (
    "Yeh ek college lecture hai. Professor Hindi aur English dono mix karke bolte hain. "
    "For example: toh yeh equation solve karenge, uske baad hum derivative nikalenge."
)

CHUNK_SECONDS = 120
SAMPLE_RATE = 16000

NOTES_PROMPT = """You are given a raw transcript of a college lecture. The professor speaks a mix of Hindi and English, so the transcript is code-switched and contains transcription errors, false starts, and filler.

Write study notes for a student who slept through the class and needs to actually learn this material.

Output GitHub-flavored Markdown with these sections:

## Summary
3-5 sentences on what this lecture covered.

## Key Points
The substantive content, as nested bullets. Include every formula, definition, derivation step, named example, and worked problem. This is the section the student studies from, so favour completeness over brevity.

## Terms
Any Hindi word or phrase the professor used for a technical idea, with its English meaning. Skip this section if there are none.

## Questions
5-10 exam-style questions covering the material, hardest last. Put answers in a collapsible block after each question:
<details><summary>Answer</summary>

...answer here...

</details>

## Flagged
Anything the transcript garbled badly enough that you had to guess, quoted with your best reading. Skip this section if nothing was unclear.

Rules:
- Start directly at "## Summary". Do not add a title heading of your own; the file already has one.
- Write ALL mathematics as LaTeX: $...$ inline, $$...$$ for display equations. Never write maths as
  plain text like "lim(h->0) [f(x+h)-f(x)]/h", it is rendered with KaTeX and plain text stays ugly.
- Write the notes in {notes_lang}.
- Preserve technical terms in English exactly as a textbook would write them.
- Where the transcript is clearly a mis-transcription of a known technical term, silently correct it.
- Never invent content that is not in the transcript. If the lecture was thin, the notes are short.
"""

# $ per million tokens, (input, output). Used for the spend readout and the
# --max-cost guard; an unlisted model falls back to the priciest rates so a
# guess never under-reports what a run cost.
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-fable-5-1": (10.0, 50.0),
}
FALLBACK_PRICE = (10.0, 50.0)


def price_of(model):
    return PRICES.get(model, FALLBACK_PRICE)


NOTES_LANG = {
    "english": "English",
    "hinglish": "romanized Hinglish, the way the professor actually speaks, keeping all technical terms in English",
}

# MANIT Bhopal, B.Tech I Sem 2026-27, Group-ST Section I.
# Codes and names from the Scheme page of the institute timetable.
# code -> (folder name, filename aliases used for auto-filing)
SUBJECTS = {
    "MC1101": ("Mathematics-1", ["maths", "math", "mathematics", "mc1101"]),
    "CY1107": ("Engineering-Chemistry", ["chem", "chemistry", "engg-chem", "cy1107"]),
    "EE1108": ("Basic-Electrical-Electronics", ["beee", "electrical", "electronics", "ee1108"]),
    "ME1109": ("Manufacturing-Science", ["manufact", "manufacturing", "me1109"]),
    # MANIT calls it Environmental Science; students call it EVS or
    # Environmental Studies. Same course, CY1110.
    "CY1110": ("Environmental-Science",
               ["envsci", "env-sci", "environmental", "environmental-studies", "evs", "cy1110"]),
    "BS1111": ("Biology-for-Engineers", ["bio", "biology", "bs1111"]),
    "HS1112": ("Indian-Knowledge-Systems", ["iks", "hs1112"]),
    "EE1125": ("BEEE-Lab", ["beee-lab", "ee1125"]),
    "CY1126": ("Engineering-Chemistry-Lab", ["chem-lab", "cy1126"]),
    "ME1127": ("Manufacturing-Science-Lab", ["ms-lab", "me1127"]),
    "SA1143": ("NSS-Yoga-UHV", ["nss", "yoga", "uhv", "sa1143", "sa1144", "sa1145"]),
    "NC1151": ("NCC", ["ncc", "nc1151"]),
    # Group-MT, which is Sections A-E. Same page of the same timetable; they
    # are here for the same reason the twelve above are -- a folder name and a
    # set of aliases a filename can be filed under. MC1101 and NC1151 are on
    # both scheme pages and are not repeated.
    "PY1102": ("Physics", ["physics", "phy", "py1102"]),
    "CE1103": ("Engineering-Mechanics", ["engg-mech", "mechanics", "ce1103"]),
    "ME1104": ("Engineering-Graphics", ["engg-graphics", "graphics", "me1104"]),
    "CS1105": ("Computer-Programming", ["comp-prog", "programming", "cs1105"]),
    "HS1106": ("Communication-Skills", ["comm-skills", "communication", "hs1106"]),
    "CE1121": ("Engineering-Mechanics-Lab", ["eml", "ce1121"]),
    "PY1122": ("Physics-Lab", ["phy-lab", "physics-lab", "py1122"]),
    "ME1123": ("Engineering-Graphics-Lab", ["graphics-lab", "me1123"]),
    "CS1124": ("Computer-Programming-Lab", ["cp-lab", "cs1124"]),
    "HS1128": ("Language-Lab", ["lang-lab", "language-lab", "hs1128"]),
    "SA1141": ("Life-Skill-Management", ["lsm", "sa1141"]),
    "SA1142": ("Physical-Education", ["phe", "sa1142"]),
}


def resolve_subject(name):
    """Match a user-typed subject to a code. Accepts a code or any alias."""
    key = name.strip().lower().replace("_", "-").replace(" ", "-")
    for code, (_, aliases) in SUBJECTS.items():
        if key == code.lower() or key in aliases:
            return code
    raise SystemExit(
        f"unknown subject {name!r}. Known: {', '.join(SUBJECTS)}\nRun `notes.py subjects` to list them."
    )


def guess_subject(filename):
    """Infer a subject from a filename, or None if it is absent or ambiguous.

    Longest alias wins so 'chem-lab' beats 'chem'; a tie between two different
    subjects returns None rather than filing it somewhere arbitrary.
    """
    stem = Path(filename).stem.lower().replace("_", "-").replace(" ", "-")
    hits = []
    for code, (_, aliases) in SUBJECTS.items():
        for alias in [code.lower()] + aliases:
            if alias in stem:
                hits.append((len(alias), code))
    if not hits:
        return None
    hits.sort(reverse=True)
    best = hits[0][0]
    winners = {code for length, code in hits if length == best}
    return winners.pop() if len(winners) == 1 else None


# Course material handed to the model alongside the transcript. Raw bytes, so
# the base64 expansion still leaves plenty of room under the 32MB request cap.
CONTEXT_EXTS = {".pdf", ".txt", ".md"}
MAX_CONTEXT_BYTES = 8 * 1024 * 1024


def subject_context(library, code, limit_bytes=MAX_CONTEXT_BYTES):
    """Build document blocks from a subject's uploads, newest first.

    Returns (blocks, included, skipped). Anything that would push past the byte
    budget is skipped and named, never silently dropped -- a lecture summarised
    against half its slides should say so.
    """
    import base64

    folder = Path(library) / f"{code}-{SUBJECTS[code][0]}" / "uploads"
    if not folder.is_dir():
        return [], [], []

    blocks, included, skipped, used = [], [], [], 0
    for f in sorted(folder.iterdir(), key=lambda p: -p.stat().st_mtime):
        if f.suffix.lower() not in CONTEXT_EXTS or not f.is_file():
            continue
        size = f.stat().st_size
        if used + size > limit_bytes:
            skipped.append(f.name)
            continue
        if f.suffix.lower() == ".pdf":
            source = {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(f.read_bytes()).decode(),
            }
        else:
            source = {
                "type": "text",
                "media_type": "text/plain",
                "data": f.read_text(errors="replace"),
            }
        blocks.append({"type": "document", "source": source, "title": f.name})
        included.append(f.name)
        used += size

    return blocks, included, skipped


CONTEXT_RULES = """
You are also given this subject's course material -- the professor's slides, PDFs and notes.
Use it to:
- Correct technical terms the transcript mangled, using the material's spelling and notation.
- Match the professor's symbols, variable names and terminology rather than generic textbook ones.
- Fill a gap where the audio was unclear but the material makes the intended point obvious.
- Under Flagged, note anywhere the lecture and the material genuinely disagree.

Do NOT pull in topics the material covers but this lecture did not. These are notes for one
class, not a summary of the course. If the lecture only reached slide 4, the notes stop there.
"""


def subject_dir(library, code, kind):
    """library/CY1107-Engineering-Chemistry/lectures|uploads/"""
    d = Path(library) / f"{code}-{SUBJECTS[code][0]}" / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def dedupe_path(dest):
    """The next free name at this path, never the name itself if it is taken.

    Two students naming a file "Unit 3 Notes" without knowing about each
    other used to be a silent overwrite: do_upload wrote straight to `dest`
    and replaced whatever was already there, no error, no trace. A rename
    makes a sensible shared name far more likely than a camera's own filename
    ever was, so this is the one place that has to hold: an upload only ever
    adds to the shelf, it never destroys what was already on it.

    A small window between this check and the file actually landing is not
    closed here -- two uploads of the exact same name in the same instant are
    rare enough in a 110-person section that a lock file would be more code
    than the risk is worth.
    """
    if not dest.exists():
        return dest
    stem, suffix, parent = dest.stem, dest.suffix, dest.parent
    n = 2
    while (parent / f"{stem} ({n}){suffix}").exists():
        n += 1
    return parent / f"{stem} ({n}){suffix}"


LOG_PATH = None  # set by serve(); None means console only


def log(msg, tag="", indent=0):
    """One line of progress. Timestamped, aligned, and kept on disk.

    Everything the tool does goes through here so the console, the log file
    and the phone all tell the same story.
    """
    import datetime

    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"{stamp}  {'  ' * indent}{tag:<12}{msg}" if tag else f"{stamp}  {'  ' * indent}{msg}"
    print(line, file=sys.stderr, flush=True)
    if LOG_PATH:
        try:
            with open(LOG_PATH, "a") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def hhmm(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".mp4", ".mov", ".aac", ".ogg", ".opus", ".flac", ".mkv", ".webm"}

# What an upload may weigh, enforced on the server before a byte of the body is
# read. A browser check is decoration: curl does not run it. 100MB is about
# three hours of phone audio; 25MB is a fat slide deck. Both numbers reach the
# page below by substitution, so the limit on screen cannot drift from the one
# that refuses you.
MAX_AUDIO_BYTES = 100 * 1024 * 1024
MAX_DOC_BYTES = 25 * 1024 * 1024


def mb(n, up=False):
    """Megabytes for a person. `up` rounds towards the next tenth, which is
    what an over-size upload needs: 104857601 bytes reading back as "100.0 MB,
    over the 100.0 MB limit" is a refusal that looks like a bug."""
    import math

    size = n / 1048576
    return f"{math.ceil(size * 10) / 10 if up else size:.1f} MB"




def collect_audio(paths):
    """Expand directories into the audio/video files inside them."""
    found = []
    for p in paths:
        if p.is_dir():
            found += sorted(f for f in p.rglob("*") if f.suffix.lower() in AUDIO_EXTS)
        elif p.exists():
            found.append(p)
        else:
            print(f"missing: {p}", file=sys.stderr)
    return found


# Whisper confuses Hindi with its acoustic neighbours and then writes Hindi
# speech in Arabic or Gurmukhi script, which is unreadable garbage. The
# professors speak Hindi and English, so anything else is a misdetection.
EXPECTED_LANGS = {"en", "hi"}
HINDI_CONFUSABLE = {"ur", "pa", "ne", "sa", "mr", "bn", "gu", "fa", "ar", "ps", "sd"}


def transcribe(audio_path, model, language, verbose=True, checkpoint=None, on_progress=None):
    """Transcribe in chunks, re-detecting language each chunk.

    Whisper detects language once from the first 30 seconds and applies it to the
    whole file, which mangles a lecture that switches between Hindi and English.
    Chunking forces a fresh detection every couple of minutes.

    Chunk boundaries land mid-sentence, so each chunk's last segment is dropped
    and the next chunk starts where the last *kept* segment ended. Whisper's own
    segmentation picks the cut point, which keeps seams on sentence boundaries.
    """
    import mlx_whisper
    from mlx_whisper.audio import load_audio

    audio = load_audio(str(audio_path), sr=SAMPLE_RATE)
    total = len(audio)
    step = CHUNK_SECONDS * SAMPLE_RATE
    repo = WHISPER_REPOS[model]

    t_start = time.time()
    pos, segments, languages = 0, [], []
    # Resume a run that was interrupted partway through an hour-long lecture.
    if checkpoint and checkpoint.exists():
        import json

        state = json.loads(checkpoint.read_text())
        pos = state["pos"]
        segments = [(s, t) for s, t in state["segments"]]
        languages = state["languages"]
        if verbose and pos:
            print(f"  resuming at {pos / SAMPLE_RATE / 60:.0f} min", file=sys.stderr)

    while pos < total:
        window = audio[pos : pos + step]
        if len(window) < SAMPLE_RATE:  # under a second of tail audio, nothing to hear
            break
        is_last = pos + step >= total

        def run(lang):
            return mlx_whisper.transcribe(
                window,
                path_or_hf_repo=repo,
                language=lang,  # None => detect this chunk on its own
                initial_prompt=HINGLISH_PROMPT,
                condition_on_previous_text=False,  # one bad chunk must not poison the rest
                verbose=None,
            )

        result = run(language)
        detected = result.get("language", "?")
        if language is None and detected in HINDI_CONFUSABLE:
            # Redo just this chunk as Hindi. Cheap: only the misdetected ones.
            log(f"{detected} looks like Hindi misheard, redoing this chunk", "retry", 1)
            result = run("hi")
            detected = "hi*"

        chunk_segs = result.get("segments") or []
        if not chunk_segs:
            pos += step
            continue

        # Drop the trailing segment unless this is the final chunk; it was cut off.
        keep = chunk_segs if is_last or len(chunk_segs) == 1 else chunk_segs[:-1]
        offset = pos / SAMPLE_RATE
        for seg in keep:
            segments.append((offset + seg["start"], seg["text"].strip()))
        languages.append(detected)

        done_sec = min((pos + step) / SAMPLE_RATE, total / SAMPLE_RATE)
        if on_progress:
            on_progress(done_sec, total / SAMPLE_RATE, detected)
        if verbose:
            pct = int(done_sec / (total / SAMPLE_RATE) * 100)
            eta = (time.time() - t_start) / max(done_sec, 1) * (total / SAMPLE_RATE - done_sec)
            log(f"{pct:>3}%  {int(done_sec)//60:>2}/{int(total/SAMPLE_RATE)//60}min  "
                f"{detected:<4} eta {hhmm(eta):<7} {keep[-1]['text'].strip()[:52]}",
                "transcribe", 1)

        advance = int(keep[-1]["end"] * SAMPLE_RATE)
        pos += advance if advance > SAMPLE_RATE else step

        if checkpoint:
            import json

            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            checkpoint.write_text(
                json.dumps({"pos": pos, "segments": segments, "languages": languages})
            )

    return segments, languages


def drop_repeats(segments):
    """Strip Whisper's repetition hallucinations, which it emits on silence.

    Two conservative rules only, because a professor genuinely does repeat
    themselves: drop a segment identical to the one immediately before it, and
    drop a segment that is nothing but the same word twice or more ("प्रस्तुति
    प्रस्तुति"). Identical lines far apart are left alone.
    """
    out = []
    for start, text in segments:
        if out and text == out[-1][1]:
            continue
        words = text.split()
        if len(words) >= 2 and len(set(words)) == 1:
            continue
        out.append((start, text))
    return out


def format_transcript(segments):
    lines = []
    for start, text in segments:
        if text:
            lines.append(f"[{int(start) // 60:02d}:{int(start) % 60:02d}] {text}")
    return "\n".join(lines)


def lecture_title(stem, code):
    return stem + (f", {SUBJECTS[code][0].replace('-', ' ')}" if code else "")


def lecture_note(stem, code, notes, transcript):
    """The markdown file a finished lecture becomes.

    One function because there are two callers now: process() here on this
    machine, and the /worker/done route when a remote worker sends back the
    same two strings. A second copy of this shape is a second file format.
    """
    return (f"# {lecture_title(stem, code)}\n\n{notes}\n\n---\n\n"
            f"<details><summary>Full transcript</summary>\n\n"
            f"```\n{transcript}\n```\n\n</details>\n")


def make_notes(transcript, notes_lang, model, context=()):
    import anthropic

    client = anthropic.Anthropic()
    system = NOTES_PROMPT.format(notes_lang=NOTES_LANG[notes_lang])
    if context:
        system += CONTEXT_RULES
    kwargs = dict(
        model=model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": [*context, {"type": "text", "text": transcript}]}],
    )
    try:
        msg = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
        )
    except (TypeError, anthropic.BadRequestError):
        msg = client.messages.create(**kwargs)  # account lacks the fallback beta

    if msg.stop_reason == "refusal":
        raise SystemExit("Claude declined to summarize this transcript.")

    text = "".join(b.text for b in msg.content if b.type == "text")
    return text, msg.usage


def destination(path, args, kind):
    """Where output goes: an explicit --outdir wins, else the subject's library folder."""
    if args.outdir:
        args.outdir.mkdir(parents=True, exist_ok=True)
        return args.outdir, None
    code = resolve_subject(args.subject) if args.subject else guess_subject(path.name)
    if not code:
        raise SystemExit(
            f"can't tell which subject {path.name!r} belongs to.\n"
            f"Pass --subject (e.g. --subject chem), or --outdir to skip the library."
        )
    return subject_dir(args.library, code, kind), code


def add_files(args):
    """File your own notes/slides/PDFs into the right subject folder."""
    import shutil

    for path in args.files:
        if not path.exists():
            print(f"missing: {path}", file=sys.stderr)
            continue
        out, code = destination(path, args, "uploads")
        target = out / path.name
        if target.exists() and target.stat().st_size == path.stat().st_size:
            print(f"{path.name} -> already in {code}, skipped", file=sys.stderr)
            continue
        shutil.copy2(path, target)
        print(f"{path.name} -> {target}", file=sys.stderr)


def search(args):
    """Grep every note and cached transcript in the library."""
    needle = args.query.lower()
    hits = 0
    for md in sorted(Path(args.library).rglob("*.md")):
        subject = md.relative_to(args.library).parts[0]
        for n, line in enumerate(md.read_text().splitlines(), 1):
            if needle in line.lower():
                hits += 1
                print(f"{subject}/{md.name}:{n}: {line.strip()[:140]}")
    sys.stdout.flush()  # keep the count after the matches when piped
    print(f"\n{hits} match{'es' if hits != 1 else ''}", file=sys.stderr)


# Offline reading. Two things only: the shell itself, so a reload with no
# signal still loads the app instead of the browser's own "no internet" page,
# and /data, so the library a student already opened -- their notes, their
# timetable, their attendance -- is still there in a corridor with no wifi.
#
# Network-first, cache as a fallback, never the other way round: a student
# with a connection must always see what is actually on the server, and the
# cache exists only for the moment there is nothing else to answer with.
# Nothing else is touched -- every POST (a vote, a mark, a doubt) needs the
# network to mean anything and is left to fail on its own, honestly, rather
# than pretend to work and lose what was typed.
#
# ponytail: CACHE is a hardcoded version, not derived from a build step this
# app does not have. Bump the suffix on a deploy that changes /data's shape or
# the shell's markup, so a phone holding the old one is not stuck comparing
# fields that no longer exist -- activate() below deletes anything that does
# not match the current name, so a bump is the whole migration.
SW_JS = r"""
const CACHE = 'recarve-v1';
const KEEP = ['/', '/index.html', '/data'];

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const { request } = e;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || !KEEP.includes(url.pathname)) return;

  e.respondWith(
    fetch(request).then((res) => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(request, copy));
      }
      return res;
    }).catch(() => caches.match(request).then(hit => hit || Response.error()))
  );
});
"""

# Raw string: this is JavaScript, and its backslash escapes are not Python's.
def _themable(page: str) -> str:
    """Fill in __PALETTES__ with the page's own two palettes, under a choice.

    The app follows the phone and always has, and that stays the default. A
    student who wants it fixed one way sets `data-theme` on <html> and these
    two blocks take over.

    They are not written by hand. The light palette is the page's first
    `:root{...}` and the dark one is the `:root{...}` inside the
    prefers-color-scheme query, and this lifts both back out and re-emits them
    under an explicit selector -- so there is exactly ONE copy of each palette
    in the source. Copied by hand there would be four, and the two nobody
    looks at are the two that go stale: a colour fixed for the students on
    'system' and still wrong for everybody who chose.

    `:root[data-theme=...]` and not `:root` on purpose: this page has exactly
    two `:root{` blocks, light then dark, and test_the_ink_reads_in_both_themes
    reads the theme off that pair by splitting on that literal.
    """
    light = re.search(r"^:root\{\n(.*?)\n\}", page, re.S | re.M).group(1)
    dark = re.search(
        r"^@media \(prefers-color-scheme:dark\)\{\n  :root\{\n(.*?)\n  \}\n\}",
        page, re.S | re.M).group(1)
    dark = "\n".join(ln[2:] if ln.startswith("  ") else ln for ln in dark.split("\n"))
    return page.replace("__PALETTES__",
                        ':root[data-theme="light"]{\n' + light + "\n}\n"
                        ':root[data-theme="dark"]{\n' + dark + "\n}")


# Raw string: this is JavaScript, and its backslash escapes are not Python's.
PAGE = _themable(r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" id="tc" content="#faf9f4">
<title>recarve · Section I</title>
<!-- The theme, settled before anything is drawn. It is four lines and it is
     inline and blocking on purpose: read from storage, stamped on <html>, and
     the whole page is painted once in the right colours. Deferred, or moved
     into the script at the bottom with everything else, and a student who
     chose dark gets a white flash on every single load -- which is the one
     thing a dark theme exists to prevent. 'system' is the default and stamps
     nothing, so the media query below stays in charge.
     One line, and it has to stay one line: the suite lifts the app's script
     out of this page with a greedy match between the first `\n<script>\n`
     and the last `\n</script>`, so a second block written across lines up
     here swallows all the markup in between and parses as nothing. -->
<script>try{const t=localStorage.getItem('theme');if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch(e){}</script>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600;700&amp;family=Kalam:wght@700&amp;display=swap">
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/15.0.7/marked.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.11/katex.min.css">
<script defer src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.11/katex.min.js"></script>
<script defer src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.11/contrib/auto-render.min.js"></script>
<style>
:root{
  /* The landing page's paper and ink, so the door and the room behind it
     are the same place. */
  --bg:#faf9f4; --surface:#f3f2ea;
  /* The plane the map stands on. A rail painted in --bg beside a page
     painted in --bg is not a rail, it is text in the margin -- so it gets
     a ground of its own. On paper it steps back to the card grey; in the
     dark it steps DOWN past the page rather than up, because an elevated
     surface reads as something that just opened and a rail never opened. */
  --rail:#f3f2ea;
  --fg:#16183d; --mut:#5b6070; --line:#e6e4da;
  /* One accent, and it is a purple. Every member's action is this colour and
     nothing else in the app is: an admin's power is ink, an error is the one
     red, a term below 75% is amber. Deep enough to carry white at 7.3:1 and to
     read on the paper at 7.1:1, which is what lets it be used this sparingly
     and still be the thing your eye goes to. */
  --accent:#6534c9; --accent-fg:#fff;
  /* Admin ink. Red already means error, so power is not red: it is the page's
     own ink, filled. Accent is every member's action, grey is neutral, ink is
     the handful of things only an admin may press. 14.5:1 either way round. */
  --admin:#232733; --admin-fg:#faf9f4;
  --sat:62%; --lum:38%; --chip-lum:94%; --chip-text:28%;
  /* Below 75% attendance. Not red: red is an error the app made, and this is
     a fact about a term that is still going. Amber, and never carrying the
     state on its own -- the words "Below 75%" sit in it. */
  --warn:#7a4a00; --warn-bg:#fdf1dc;
  /* One red, and the only red. #e5484d was 3.7:1 on the light ground and
     failing the words it was carrying. This clears 4.5 on BOTH grounds an
     error is ever set on here -- 5.4:1 on the paper and 4.95:1 on a surface,
     which is where a failed transcription says so. It lightens in the dark
     for the same reason every other ink does. */
  --err:#c62b41;
  /* The boundary of a control that draws no fill. --line is the hairline
     between two rows: 1.24:1 on the ground, where WCAG 1.4.11 wants 3:1 for
     the edge of something you can press. Every outlined button in the app was
     drawing that hairline, so Rename, Remove, the two attendance marks and
     Cancel all read as grey text rather than as buttons. Not a new colour --
     it is --mut, the ink those same buttons write in, thinned until it is an
     edge and not a word: 3.5:1 on the paper and 5.0:1 in the dark, clearing
     1.4.11's 3:1 with the room 70% did not have. */
  --edge:color-mix(in srgb,var(--mut) 80%,transparent);
  /* The one gesture the landing page makes that this app never did: violet
     through orchid into pink, across the words that name what a screen is
     about. Three stops rather than two so the middle of a short word is not
     already at the pink end. Pressed onto paper here -- the gate's own
     #ac93ff/#c58bff/#ff7ab6 are 2.1:1 on this ground and would fail every
     word they carried -- and the darkest of these three is the accent the
     whole app is built on, so the family is the same family. 5.75:1 at worst
     on the paper and 5.31:1 at worst on a surface card, both clearing the
     4.5:1 that any of these three ever has to carry. */
  --g1:#6534c9; --g2:#9629cc; --g3:#c21362;
  --tap:44px;
  /* The two columns that stand before the reading pane on a wide screen,
     written once. The dock and the + both have to clear them, and the sum
     of the two used to be the literal 552 copied into three rules -- so
     widening the rail by a hair moved the + and left the dock behind. */
  --railw:252px; --colw:62rem;
  /* What the + button covers, reserved at the bottom of everything that
     scrolls. It is 58px across with its bottom edge 76px up, so it reaches
     134; 142 puts a row's own 8px under it as well. Written once here
     because it was written twice in the sheet and missed everywhere else,
     which is how a confession, a note's body and a card's buttons all ended
     up half under a purple circle. */
  --fabclear:calc(142px + env(safe-area-inset-bottom));
  /* 4 (.rows) + 12 (.row) + 3 (.tick) + 12 (gap): where a row's words begin.
     Anything standing in for a row lines up with them, and everything that is
     not a row lines up with the section heading at 16. */
  --hang:31px;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0f1115; --surface:#171a20;
    --rail:#0b0d11;
    --fg:#e7e9ee; --mut:#98a0ad; --line:#262a32;
    /* The same purple, lifted off a near-black ground rather than pressed
       onto paper -- and what sits ON it goes dark, because the light version
       of this colour cannot carry white. */
    --accent:#ac93ff; --accent-fg:#0f1115;
    /* Ink inverts with the paper: near-black on near-black is not a slab. */
    --admin:#dfe4f0; --admin-fg:#0f1115;
    --sat:48%; --lum:70%; --chip-lum:22%; --chip-text:78%;
    --warn:#f0bd6a; --warn-bg:#2b2114;
    --err:#ef5f63;
    /* The landing page's three stops, to the digit: in the dark the app is
       standing on the same near-black the gate is, so nothing has to be
       darkened to be readable. 7.53:1 at worst on the ground. */
    --g1:#ac93ff; --g2:#c58bff; --g3:#ff7ab6;
  }
}
/* Choosing for yourself. The phone's own setting is the default and the two
   blocks above are it; these two are the same two palettes under an explicit
   choice, and they are FILLED IN AT IMPORT from those blocks rather than
   copied here -- a hand-copied palette is a palette that drifts, and the
   drift would be a colour that is only wrong for the students who picked. */
__PALETTES__
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%;color-scheme:light dark}
html[data-theme="light"]{color-scheme:light}
html[data-theme="dark"]{color-scheme:dark}
body{
  margin:0;background:var(--bg);color:var(--fg);
  font:16px/1.65 Figtree,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  overflow-wrap:break-word;
}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
/* ---- Motion, all of it, in one place. Every one of these answers something
   somebody did: a press, a state changing, a screen arriving. Nothing moves
   on its own and nothing overshoots. 140ms for a control answering, 180ms for
   a screen -- long enough to be seen, too short to be waited on -- and
   ease-out, so the thing is already most of the way there by the time the eye
   finds it. A press is the exception: it lands at 0s and only fades on the
   way back out, because a highlight that takes 140ms to arrive under a thumb
   feels like a slow phone. The query below turns off every line of it. */
button,a,summary,select,input,textarea,.row,.rank,.card,.ann,.job,.tabs button{
  transition:background-color 140ms ease-out,color 140ms ease-out,
             border-color 140ms ease-out,box-shadow 140ms ease-out,
             opacity 140ms ease-out,transform 140ms ease-out}
button:active,a:active,.row:active,summary:active{transition-duration:0s}
@keyframes arrive{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@keyframes fadein{from{opacity:0}to{opacity:1}}
@keyframes riseup{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}
/* A tab you switched to. Set by route(), and only when the tab actually
   changed -- render() runs on every vote and every mark, and a screen that
   re-animates when you tick one box is a screen that flickers. */
#nav.swap,#read.swap{animation:arrive 180ms ease-out}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
[hidden]{display:none!important}   /* beats the display: on .brand and .shead */
/* Display type, and the one place a gradient is allowed. Three things carry
   it and they are the same thing three times: the subject of the screen you
   are on -- the app's own name on Home, the score on Your contributions, the
   result of a practice run. Never a heading you did not come for, never a
   control, never an ornament.
   @supports, because background-clip:text with a transparent colour is a
   number that disappears entirely on a browser that cannot do it, and a score
   you cannot read is worse than a score that is not purple.
   The screen's own name takes the size and not the gradient: it is on every
   screen, and a gradient on every screen is wallpaper. */
.brand b,.shead h2,.mine:has(.tally) .score,#qscore{
  font-size:40px;line-height:1.05;letter-spacing:-.035em;font-weight:800}
@supports ((-webkit-background-clip:text) or (background-clip:text)){
  .brand b,.dhead b,.mine:has(.tally) .score,#qscore{
    background:linear-gradient(96deg,var(--g1) 0%,var(--g2) 46%,var(--g3) 100%);
    -webkit-background-clip:text;background-clip:text;color:transparent}
}

#read{display:none}
body.reading #list{display:none}
body.reading #read{display:block}

.top{
  position:sticky;top:0;z-index:5;background:var(--bg);
  border-bottom:1px solid var(--line);
  padding:max(10px,env(safe-area-inset-top)) 16px 10px;
}
/* The top of every screen: what you are looking at, and you. The screen's own
   name is the largest type in the app, because with four tabs and three levels
   inside two of them, the cheapest way to say where somebody is standing is to
   say it. Everything else up here is quiet around it.
   At 40px the name no longer shares a line with anything. The code chip sits
   above it and the avatar beside the chip; the name takes the whole width
   under them, so "Your contributions" runs to two lines instead of ellipsising
   after four letters, which is what 26px on a shared row already had to do.
   The avatar goes to the top of that block rather than its middle: it belongs
   to the chip's line, not to the name's. */
.tophead{display:flex;align-items:flex-start;gap:12px;min-height:var(--tap);margin-bottom:4px}
.brand{display:flex;flex-wrap:wrap;align-items:baseline;gap:0 8px;flex:1;min-width:0}
.brand span{flex:1 0 100%;margin-top:2px;font-size:13px;color:var(--mut)}
/* You, and the way into everywhere else. The four tabs at the bottom are the
   four screens you cross between classes; this opens the whole map, which is
   the only way to the levels inside them -- your day, the timetable, catching
   up, what you saved, what you have put in. A glyph drawn in the page rather
   than an image or a webfont: nothing to download, nothing to go missing
   offline, and it inherits the ink around it. */
#avatar{flex:none;width:var(--tap);height:var(--tap);margin-right:-8px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;color:var(--mut)}
#avatar svg{width:24px;height:24px}
#avatar[aria-expanded="true"]{color:var(--accent);background:var(--surface)}
#avatar:active{background:var(--surface)}

/* ---- The map: a drawer on a phone, a rail on a wide screen. -------------
   Off-canvas rather than a dropdown because it is eleven rows in five named
   groups now and not four, and eleven rows hanging off a header is a column
   of links with no edges. It slides from the left, which is the side the FAB
   is not on: the + stays exactly where it was and the two never reach for the
   same corner.
   visibility, not just transform: a drawer that is only translated away is
   still in the tab order and still read out, so Tab from the search box used
   to walk off the screen into links nobody could see. visibility:hidden takes
   it out of both, and transitioning it alongside the transform is what lets
   it still slide rather than blink. */
#scrim{position:fixed;inset:0;z-index:9;background:rgba(0,0,0,.45);
  opacity:0;visibility:hidden;transition:opacity 180ms ease-out,visibility 180ms}
body.drawered #scrim{opacity:1;visibility:visible}
#drawer{position:fixed;z-index:10;left:0;top:0;bottom:0;width:min(84vw,290px);
  display:flex;flex-direction:column;background:var(--rail);
  border-right:1px solid var(--line);box-shadow:0 0 40px rgba(0,0,0,.32);
  transform:translateX(-101%);visibility:hidden;
  transition:transform 180ms ease-out,visibility 180ms;
  overflow-y:auto;overscroll-behavior:contain;
  padding-bottom:calc(16px + env(safe-area-inset-bottom))}
body.drawered #drawer{transform:none;visibility:visible}
/* The app says its own name once. On a phone that is here, at the top of the
   map; on a wide screen the header beside it stands down (see the 760 block)
   rather than printing "recarve" twice, thirty pixels apart. The section it
   belongs to sits under the name, because which section you are in is a fact
   about the whole app and not about the screen you happen to be on. */
.dhead{display:flex;align-items:flex-start;gap:12px;flex:none;
  padding:max(14px,env(safe-area-inset-top)) 12px 14px 16px}
.dhead .who{flex:1;min-width:0;display:flex;flex-direction:column}
.dhead b{font-size:20px;font-weight:700;letter-spacing:-.015em;line-height:1.15}
.dhead small{font-size:11px;font-weight:500;color:var(--mut);margin-top:3px}
#dclose{flex:none;min-width:var(--tap);min-height:var(--tap);border-radius:11px;
  font-size:13px;color:var(--mut);margin:-8px -4px 0 0}
#dclose:active{background:var(--line)}
#dnav{padding:2px 0 4px}
/* Five sections and eleven destinations. A section carries a mark, because
   five marks are what the eye scans a column by; the levels inside it do not,
   because indented under an open section they are a list being read and not a
   column being scanned -- and an icon on each would push "Points and what you
   added" onto a second line in a 252px rail. Drawn in the page rather than
   fetched, so an offline handset has the whole map and not a row of empty
   boxes. */
#drawer a,#drawer summary{display:flex;align-items:center;gap:12px;
  min-height:var(--tap);margin:1px 8px;padding:0 12px;border-radius:11px;
  font-size:16px;line-height:1.25;font-weight:500;color:var(--fg);
  text-decoration:none;list-style:none;cursor:pointer}
#drawer summary::-webkit-details-marker{display:none}
#drawer svg{flex:none;width:20px;height:20px;color:var(--mut)}
#drawer a:active,#drawer summary:active{background:var(--line)}
/* The twisty. It is the last thing in the row and the quietest thing in it:
   what a section is called is the information, and whether it happens to be
   open is something you can already see. */
#drawer summary::after{content:'';flex:none;margin-left:auto;width:7px;height:7px;
  border-right:1.6px solid var(--mut);border-bottom:1.6px solid var(--mut);
  transform:rotate(45deg) translate(-2px,-2px);
  transition:transform 140ms ease-out}
#drawer details[open]>summary::after{transform:rotate(225deg) translate(-2px,-2px)}
/* The levels inside a section, hung off one hairline so the indent is a
   visible fact and not four rows that merely start further in. */
#drawer .kids{margin:2px 0 6px 31px;padding-left:1px;border-left:1px solid var(--line)}
#drawer .kids a{margin:0 8px 0 0;padding:0 12px;font-size:16px;color:var(--mut)}
#drawer .kids a[aria-current]{color:var(--accent)}
/* A section you are standing in keeps its name in the page's own ink while
   it is open, so a collapsed rail still says which of the five you are in. */
#drawer details[data-here]>summary{font-weight:600}
#drawer details[data-here]>summary svg{color:var(--fg)}
/* Where you are, said twice: in the accent, and with the same 3px tick a row
   carries -- so it survives a grey screen and a colourblind reader, which
   colour alone does not. The fill is the accent thinned, not the card grey:
   a grey slab is what a pressed control looks like, and this is not pressed.
   The tick is the left edge of that fill rather than a bar floating beside
   it, so the lit row reads as one object. */
#drawer a[aria-current]{color:var(--accent);font-weight:600;position:relative;
  background:color-mix(in srgb,var(--accent) 11%,transparent)}
#drawer a[aria-current] svg{color:var(--accent)}
#drawer a[aria-current]::before{content:"";position:absolute;left:0;top:9px;bottom:9px;
  width:3px;border-radius:0 2px 2px 0;background:var(--accent)}
#drawer .kids a[aria-current]::before{left:-13px}
#drawer .tag{margin-left:auto}
/* The foot of the map: your face, and the light. A row, because these are the
   two things that belong to you rather than to a screen, and the bottom left
   corner is where both have lived in every app anybody here already uses.
   The light is placed ON the row rather than beside it in a flex track: the
   four rows that open out of You are a list in a 252px rail, and taking 44px
   off the column for a control that belongs to the row above them put "Your
   contributions" on two lines. The row reserves the space; the list does not
   pay for it. */
.foot{flex:none;position:relative;padding:6px 0 0;
  border-top:1px solid var(--line);margin-top:8px}
#drawer .foot summary{padding-right:52px}
/* One button and not three segments. The glyph is what the press would DO --
   a moon while the page is light -- and the label says so in words, because a
   half-moon on its own is a guess. Following the phone is still the default
   and still what an untouched install does; the first press is what makes the
   choice explicit, and from then on this is a switch with two ends. */
#themebtn{position:absolute;top:7px;right:12px;
  display:flex;align-items:center;justify-content:center;
  width:var(--tap);height:var(--tap);border-radius:11px;color:var(--mut)}
#themebtn:active{background:var(--line)}
@media (hover:hover){#themebtn:hover{background:color-mix(in srgb,var(--fg) 6%,transparent);
  color:var(--fg)}}
/* The map fills the rail and the foot sits under it, rather than the foot
   riding up under the last row on a tall screen. */
#dnav{flex:1 0 auto}
@media (hover:hover){
  #drawer a:hover,#drawer summary:hover{background:color-mix(in srgb,var(--fg) 6%,transparent)}
  #drawer a[aria-current]:hover{background:color-mix(in srgb,var(--accent) 16%,transparent)}
}

.group{padding:24px 16px 4px}
.group h2{display:inline;margin:0 0 0 8px;font-size:13px;font-weight:500;color:var(--mut)}
.shead{display:flex;flex-wrap:wrap;align-items:center;gap:0 9px;flex:1;min-width:0}
/* The screen's name, on its own line, in the app's display size. It wraps
   rather than ellipsising now that it has the whole width: at 26px on a shared
   row "Mathematics 1" showed four letters and a dot-dot-dot, and the tail of a
   subject's name is the half that says which subject it is. */
.shead h2{flex:1 0 100%;margin:2px 0 0;color:var(--fg);overflow-wrap:anywhere}
/* A section's name is the smallest thing on the screen, not the same size as
   the secondary line inside its own rows. At 13px it competed with the rows
   it was labelling, and five of them down Home read as five equal headings;
   at 11px -- the scale's floor, the size the week strip's day letters already
   take -- it recedes to what it is, which is a label on a group. --mut on the
   ground is 5.2:1 light and 7.2:1 dark: quiet, not faint. */
.sect{margin:0;padding:24px 16px 8px;font-size:11px;font-weight:600;
  letter-spacing:.02em;color:var(--mut)}
/* An inset block carries its own bottom margin, so the 24px above the next
   section's name is already part paid. Without these three the gap after a
   card is 32-40px and the gap after a row is 24px, which is the kind of
   difference you cannot name and can always see. */
.blank+.sect{padding-top:8px}
.card+.sect,.cal+.sect,.mine+.sect{padding-top:16px}
.rows{padding:4px 4px 0}
/* Hover is the one thing left in the token block for these: Tailwind would
   emit it happily, but the neighbours below share the media query and reading
   them in one place is worth more than the utilities. */
@media (hover:hover){
  .row:hover{background:var(--surface)}
  .rank:hover{background:color-mix(in srgb,var(--surface) 60%,transparent)}
  .tabs button:hover{background:var(--surface)}
  .days button:hover:not([aria-current]){background:color-mix(in srgb,var(--fg) 8%,var(--surface))}
  .vote:hover{background:color-mix(in srgb,var(--fg) 6%,var(--surface))}
  .mark button:hover,.dpick .step:hover:not([disabled]){border-color:var(--mut)}
  .dock button:hover,.qdock button:hover{filter:brightness(.96)}
}

/* The pressed states keep their own rules: color-mix() is not a utility, and
   a borderless pill states itself with an inset ring rather than an edge it
   does not otherwise have. The count inside it is still the words. */
.vote.on{box-shadow:inset 0 0 0 1px var(--accent);color:var(--accent);
  background:color-mix(in srgb,var(--accent) 13%,transparent)}
.vote[disabled]{opacity:.45}
.row-del.armed{border-color:var(--err);color:var(--err);
  background:color-mix(in srgb,var(--err) 12%,transparent)}
/* A row carrying admin controls -- vote, Rename, Remove, maybe a thumbnail --
   has more buttons than a 375px phone has room for beside a name. Without
   this the name, the only flex item allowed to shrink, collapsed to one
   letter per line. So such a row wraps: the name keeps the whole first line,
   and the controls sit together on the line under it, lined up with it. */
.row:has(.row-del){flex-wrap:wrap;row-gap:8px}
.row:has(.row-del) .name{flex:1 1 calc(100% - 90px)}
.row:has(.row-del) .vote{margin-left:15px}
.row:has(.row-del) > .vote ~ .row-del,.row:has(.row-del) > .row-del ~ .row-del{margin-left:0}
.row:has(.row-del):not(:has(.vote)) > .row-del:first-of-type{margin-left:15px}
/* Rename turns the row itself into the edit box -- no dialog, same as Remove. */
.week i{display:flex;gap:3px;height:6px}
.week i::before{content:"";width:6px;height:6px;border-radius:50%;background:transparent}
.week .has i::before{background:var(--accent)}
.week .off i::before{background:var(--err)}
.week .past b{color:var(--mut)}
.week .today{background:var(--accent);color:var(--accent-fg)}
.week .today small,.week .today b{color:var(--accent-fg)}
.week .today.has i::before{background:var(--accent-fg)}
/* The day as a timeline, Google-Calendar style: a class is as tall as it is
   long, free time is a dashed gap you can see at a glance, lunch is shaded,
   and a red line says where you are in the day right now. */
.tl{position:relative;margin:4px 16px 6px 12px;--px:1.05px}
.tl .hr{position:absolute;left:0;right:0;height:0;border-top:1px solid var(--line)}
.tl .hr span{position:absolute;left:0;top:-8px;width:40px;font-size:11px;color:var(--mut);
  font-variant-numeric:tabular-nums;background:var(--bg);padding-right:4px}
/* Opaque rather than an alpha over the page, because the words inside it have
   to be able to knock the now line out from behind them -- see .now below --
   and a knockout can only be drawn in a colour that is known. */
.tl .ev{position:absolute;left:48px;right:0;border-radius:11px;padding:6px 10px;overflow:hidden;
  display:flex;flex-direction:column;justify-content:flex-start;border:0;font:inherit;
  text-align:left;color:var(--fg);
  --fill:color-mix(in srgb,hsl(var(--h) var(--sat) var(--lum)) 20%,var(--bg));
  background:var(--fill);
  border-left:4px solid hsl(var(--h) var(--sat) var(--lum))}
/* Already happened. Said in the furniture and never in the ink: greying the
   title made the first two blocks of a day a different colour from the third
   for a reason nobody can see on a screen -- three classes, three-quarters of
   an hour apart, and the two that are over read as a different KIND of thing
   rather than as the same thing earlier. The block goes pale, the rule down
   its edge goes with it, and every class on the day is named in one ink. */
.tl .ev.done{--fill:color-mix(in srgb,hsl(var(--h) var(--sat) var(--lum)) 8%,var(--bg));
  border-left-color:color-mix(in srgb,hsl(var(--h) var(--sat) var(--lum)) 40%,var(--bg))}
.tl .free{position:absolute;left:48px;right:0;border-radius:11px;border:1.5px dashed var(--line);
  display:flex;align-items:center;justify-content:center}
.tl .lunch{position:absolute;left:48px;right:0;border-radius:11px;display:flex;
  align-items:center;justify-content:center;
  background:repeating-linear-gradient(135deg,var(--surface) 0 8px,transparent 8px 16px)}
.tl .now{position:absolute;left:40px;right:0;height:2px;background:var(--err);z-index:2}
.tl .now::before{content:"";position:absolute;left:-5px;top:-4px;width:10px;height:10px;
  border-radius:50%;background:var(--err)}
/* The one place the now line lands is the class that is running, and that is
   the one class whose name is under it: at 11:41 the rule went straight
   through "11:00-12:55 - 1 h 55 min - EE1125" and neither the time nor the
   code could be read. The words knock it out and it carries on either side
   of them -- the same thing the hour labels above already do to the hour
   rules, so a rule passing behind type is a device this screen already has.
   fit-content is what keeps the knockout the width of the words rather than
   the width of the block. */
.tl .ev b,.tl .ev small{position:relative;z-index:3;width:fit-content;max-width:100%;
  background:var(--fill);padding-right:6px;border-radius:2px}
/* --mut is 5.2:1 on the bare page, and a coloured block eats into that: on the
   block that is running right now it fell to 3.6:1, and that is the line that
   says when the class ends. The quiet ink inside a block is the block's own
   ink thinned rather than the page's grey, so it stays quieter than the title
   and still clears 4.5:1 on all eight subject colours in both themes -- 5.5:1
   at the worst of them. */
.tl .ev small{color:color-mix(in srgb,var(--fg) 70%,var(--fill))}
.rename button.primary{background:var(--accent);color:var(--accent-fg);border:0}
.row-del[disabled]{opacity:.45}

.mark button[aria-pressed="true"]{border-width:2px;font-weight:700}
.mark button.yes[aria-pressed="true"]{border-color:var(--accent);color:var(--accent);
  background:color-mix(in srgb,var(--accent) 13%,transparent)}
.mark button.no[aria-pressed="true"]{border-color:var(--fg);color:var(--fg);
  background:var(--surface)}
.mark button[disabled]{opacity:.45}
/* A class that did not happen. Dashed, because it is a hole in the week and
   not a thing anybody did. */
.row .off{display:flex;align-items:center;flex:none;min-height:var(--tap);padding:0 12px;
  border:1px dashed var(--edge);border-radius:11px;font-size:13px;color:var(--mut)}
button.off:active{background:var(--surface)}
/* attended / held / the percentage. The one figure on this screen somebody
   opened this screen for, so it leads the row rather than sharing its weight
   with "Edit your timetable". Tabular, so a column of them does not wobble. */
.row.att .name b{font-size:26px;font-weight:700;letter-spacing:-.022em;
  font-variant-numeric:tabular-nums;line-height:1.2}
/* Below 75%. The number in amber and the word beside it, in the calmest
   arrangement that still cannot be missed -- the student already knows it is
   bad, and what they need off this row is the figure and the next step, not a
   siren. It is on the NUMBER and never on the tick: the tick is the subject's
   own colour and EE1108's is amber, so a healthy EE1108 and a failing CY1107
   read identically the moment the state borrows that bar. */
.row.low .name b{color:var(--warn)}
/* The day picker, shared by the day view and the catch-up screen. Native date
   input: the fastest picker on a phone, nothing to download, and it already
   knows what a month looks like. */
.dpick{display:flex;align-items:center;gap:8px;padding:12px 16px 0}
.dpick input{flex:1;min-width:0;height:var(--tap);padding:0 12px;font:inherit;
  font-size:16px;border:1px solid var(--edge);border-radius:11px;
  background:var(--bg);color:var(--fg)}
/* Yesterday and tomorrow, at thumb size. The picker is for jumping a month;
   stepping one day is the move somebody makes walking out of a lecture, and it
   must not cost a modal wheel. */
.dpick .step{flex:none;width:var(--tap);height:var(--tap);border-radius:11px;
  border:1px solid var(--edge);background:var(--bg);color:var(--fg);
  font-size:20px;line-height:1}
.dpick .step:active{background:var(--surface)}
/* Swiped, not dragged: the bar a desktop browser draws under this strip is
   furniture for a gesture nobody makes on the phone this is used on. */
.days{scrollbar-width:none}
.days::-webkit-scrollbar{display:none}
.dpick .step[disabled]{opacity:.35}
/* .score is the first line of the card whatever the card is: your name on the
   profile, "Your details" on the form, the total on Your contributions. Only
   the last of those is a number somebody came to see, and :has(.tally) is what
   tells them apart -- the tally is only ever drawn under the total. */
.mine .score{font-size:26px;font-weight:700;letter-spacing:-.02em}
.mine:has(.tally) .score{font-variant-numeric:tabular-nums}
.mine p{margin:4px 0 0;font-size:13px;color:var(--mut)}
.tally{display:flex;gap:24px;margin-top:16px}
.tally div{font-size:13px;color:var(--mut)}
.tally b{display:block;font-size:20px;font-weight:600;color:var(--fg);
  font-variant-numeric:tabular-nums}
.mine .sub{margin:4px 0 0;font-size:16px;color:var(--mut)}
/* Yours, at the size that makes the card yours -- above the name rather than
   beside it, because the name is the 26px line this card is built around. */
.mine>.face{margin-bottom:12px}
.mine .edit{margin-top:16px;min-height:var(--tap);padding:0 16px;border-radius:11px;
  background:var(--bg);border:1px solid var(--edge);font-size:16px;font-weight:600;
  color:var(--accent)}
.mine .edit:active{background:var(--surface)}
/* A CR wears the trusted badge, because that is what a CR is plus one thing.
   Admin below is the one that looks different, and it should stay the one. */
.badge.trusted,.badge.cr{background:color-mix(in srgb,var(--accent) 16%,transparent);
  border-color:var(--accent);color:var(--accent)}

/* ---- The class board. A number, a name, a score -- ruled lines rather than
   another stack of identical rounded cards, because twenty cards is a wall and
   not a ranking. Your own row is the one filled one, so finding yourself in it
   costs no reading. */
.rank{display:flex;align-items:center;gap:12px;min-height:var(--tap);
  padding:8px 16px;border-bottom:1px solid var(--line)}
.rank .pos{flex:none;width:2.6em;font-size:13px;color:var(--mut);
  font-variant-numeric:tabular-nums}
.rank .name{flex:1;min-width:0}
.rank .name b,.rank .name small{display:block}
/* The line under the name is a summary and its first words are the whole of
   it, so that is the one that gets cut. The name itself wraps to a second
   line instead: the face beside it costs the column 44px, and a board is a
   list of people -- "Sneha Bhatt…" is not one of them. */
.rank .name small{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rank .name b{font-weight:600}
.rank .name small{font-size:13px;color:var(--mut)}
/* Earned, not decorative: only shown from Regular up, so a word here means
   somebody did the work. A keyline and the page's own ink, no medal. */
.rank .lvl{flex:none;padding:3px 8px;border-radius:7px;font-size:13px;
  font-weight:600;border:1px solid var(--line);color:var(--mut)}
.rank .pts{flex:none;min-width:2.4em;text-align:right;font-size:20px;
  font-weight:600;font-variant-numeric:tabular-nums}
.rank.you{background:var(--surface);box-shadow:inset 3px 0 0 var(--accent)}
.rank.you .pos,.rank.you .pts{color:var(--accent)}

/* ---- The notice board. Ruled entries, not another stack of rounded cards:
   a notice is a piece of writing with a title over it, and a box around every
   one of them turns six notices into six identical boxes. Pinned is a rule
   down the left edge in the accent -- the same weight as a row's tick, so the
   two screens read as one app. */
.ann{padding:16px;border-bottom:1px solid var(--line)}
.ann.pin{border-left:3px solid var(--accent);padding-left:13px}
.ann+.ann{padding-top:20px}
/* Set back by its furniture, not by its ink: an alpha on the card dimmed
   the one word that explains the state. The rule down the edge goes grey,
   the flag takes the page's muted ink, and every string still reads. */
.ann.gone{border-left:3px solid var(--line);padding-left:13px}
.ann h3{margin:0;font-size:20px;line-height:1.3;font-weight:700;letter-spacing:-.015em}
.ann .meta{margin:4px 0 0;font-size:13px;color:var(--mut)}
.ann .flag{font-weight:600;color:var(--accent);
  background:color-mix(in srgb,var(--accent) 14%,transparent)}
.ann.gone .flag{color:var(--mut)}
.ann.gone .flag{background:var(--surface)}
.ann .md{margin-top:12px;font-size:16px}
.ann .md>:first-child{margin-top:0}
.ann .md>:last-child{margin-bottom:0}
.ann .md p{margin:12px 0}
.ann .md ul,.ann .md ol{padding-left:24px;margin:12px 0}
.ann .md li{margin:4px 0}
.ann .md h1,.ann .md h2,.ann .md h3{font-size:16px;font-weight:700;margin:16px 0 4px}
.ann .md a{color:var(--accent)}
.ann .md code{background:var(--surface);padding:2px 5px;border-radius:7px;font-size:.92em}
.ann .md pre{background:var(--surface);padding:12px;border-radius:11px;overflow-x:auto;font-size:13px}
.ann .md img{max-width:100%;height:auto}
/* A pasted table is the one thing in a body that can be wider than a phone.
   It scrolls inside itself rather than taking the page sideways with it. */
.ann .md table{display:block;overflow-x:auto;min-width:0}
/* Editing and hiding are admin ink, the same ink as every other control only
   an admin may press. */
.ann .acts{display:flex;gap:8px;margin-top:16px}
/* Outlined rather than a filled slab: two inked slabs under every notice an
   admin owns outweigh the notice itself. Ink on the page's own paper, keyed
   by a keyline -- distinct from every member's control, and 14.5:1 either way
   round. */
.ann .acts button{min-height:var(--tap);padding:0 16px;border-radius:11px;
  background:var(--bg);border:1px solid var(--admin);color:var(--admin);
  font-size:13px;font-weight:600}
.ann .acts button:active{background:var(--surface)}

/* ---- Doubts: the thread under a note. Ruled entries, not cards -- a question
   and the answers to it are one piece of writing between people, and a box
   around each one turns a conversation into a list of unrelated things. The
   answers are set in from the question by a rule, which is the only nesting
   this page has and the only nesting the table allows. */
#doubts{max-width:70ch;margin:0 auto;padding:0 18px var(--fabclear)}
/* The same thread, drawn in three places now: under a lecture note, under an
   uploaded file, and nowhere else that is not one of those two. The rules
   below key off the class rather than the id, because a comment section is
   the same piece of writing between people wherever it hangs. */
.thread{max-width:70ch}
/* The same thread, standing in a tab rather than under a note: it needs the
   screen's own margins, which #doubts gets from its own rule. */
.thread.inset{margin:0 auto;padding:0 16px}
.thread.inset h2{margin-top:20px}
.filethread{padding:0 0 10px}
.thread h2{margin:38px 0 0;font-size:20px;line-height:1.3;letter-spacing:-.012em;
  font-weight:600;padding-bottom:7px;border-bottom:1px solid var(--line)}
.thread .quiet{padding:14px 0 0}
.dbt{padding:16px 0;border-bottom:1px solid var(--line)}
/* The question's own words, its byline and its buttons, in one box -- so the
   answer form opens under the question it answers and above the answers
   already there, rather than at the end of everything. */
.dbt>.what{min-width:0}
/* Somebody's typing, drawn as typing: pre-wrap keeps their line breaks and
   textContent is what puts it there. Nothing in this block is ever parsed. */
.said{margin:0;font-size:16px;white-space:pre-wrap;overflow-wrap:anywhere}
.dbt>.what>.said{font-weight:600;line-height:1.5}
.dbt .meta{margin:4px 0 0;font-size:13px;color:var(--mut)}
/* A byline with a face on the front of it -- and only one that has a face, so
   every other .meta in this app is left as the line of small grey it was. */
.meta:has(.face){display:flex;align-items:center;gap:8px}
.ans{display:flex;gap:12px;align-items:flex-start;
  margin:16px 0 0 2px;padding:0 0 0 12px;border-left:2px solid var(--line)}
.ans .what{flex:1;min-width:0}
/* The one the class voted up. Said in the rule, never by dimming the others,
   which are still answers worth reading. */
.ans.top{border-left-color:var(--accent)}
/* The bot's. It sits in the answer column because that is what it is an
   answer to, and it is dashed because it is the one thing on this screen
   nobody in the section stands behind and nothing keeps: close the note and
   it is gone. Grey, never the accent -- the accent on .ans.top is the class
   saying this one is right, and no machine gets to borrow that. */
.ans.ai{border-left-style:dashed;border-left-color:var(--mut)}
.ans.ai .meta{color:var(--mut);font-size:13px}
.thread .acts{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.thread .acts button,.post .acts button,.msg .acts button{
  min-height:var(--tap);padding:0 16px;border-radius:11px;
  background:var(--bg);border:1px solid var(--edge);color:var(--accent);
  font-size:13px;font-weight:600}
/* Taking down somebody else's is an admin act, marked as one: the page's own
   ink on a keyline, the same treatment as every other admin control. */
.thread .acts button.adm,.post .acts button.adm,.msg .acts button.adm{
  border-color:var(--admin);color:var(--admin)}
.thread .acts button:active,.post .acts button:active,.msg .acts button:active{
  background:var(--surface)}
/* A post's own buttons sit under the words, not beside them. */
.post .acts,.msg .acts{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.askbox{padding:14px 0 2px}
.askbox textarea{width:100%;min-height:5.5em;font-size:16px;line-height:1.6;
  padding:12px;border:1px solid var(--edge);border-radius:11px;
  background:var(--bg);color:var(--fg);font-family:inherit;resize:vertical}
.askbox textarea::placeholder{color:var(--mut)}
.askbox .err{margin:8px 0 0;font-size:13px;color:var(--err);min-height:1.2em}
.askbox button{display:block;width:100%;min-height:var(--tap);padding:0 18px;
  border-radius:11px;background:var(--accent);color:var(--accent-fg);
  font-size:16px;font-weight:600}
.askbox button:active{opacity:.75}

/* ---- The wall, and the room. Both are somebody's typing, so both borrow
   .said and .askbox above rather than growing type of their own. A post is a
   ruled entry like a doubt; a message is a line in a conversation and is set
   tighter, because thirty of them are read at once. */
.wall{max-width:70ch;margin:0 auto;padding:0 16px}
.post{display:flex;gap:12px;align-items:flex-start;
  padding:16px 0;border-bottom:1px solid var(--line)}
.post .what{flex:1;min-width:0}
.post .meta{margin:4px 0 0;font-size:13px;color:var(--mut)}
.shots{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.shots img{width:96px;height:96px;object-fit:cover;border-radius:11px;
  border:1px solid var(--line)}
.room{max-width:70ch;margin:0 auto;padding:0 16px}
.room .log{max-height:52vh;overflow-y:auto;overscroll-behavior:contain;
  padding:4px 0 2px}
.msg{padding:8px 0;border-bottom:1px solid var(--line)}
.msg .meta{margin:4px 0 0;font-size:13px;color:var(--mut)}
.msg.me .said{color:var(--accent)}
.msg .acts button{min-height:0;padding:2px 0;background:none;border:0;
  font-size:13px;color:var(--mut)}
/* One line and a Send, not the five-line box a question gets: a message is a
   sentence and the keyboard already takes half the screen. */
.saybox{display:flex;gap:8px;padding:12px 0 4px}
.saybox input{flex:1;min-width:0;min-height:var(--tap);font-size:16px;
  padding:0 12px;border:1px solid var(--edge);border-radius:11px;
  background:var(--bg);color:var(--fg)}
.saybox button{flex:none;min-height:var(--tap);padding:0 18px;border-radius:11px;
  background:var(--accent);color:var(--accent-fg);font-size:16px;font-weight:600}
.saybox button:active{opacity:.75}

/* The composer: a title, a body, and whether it sits at the top. */
.compose{padding:8px 16px 16px}
.compose label{display:block;font-size:13px;color:var(--mut);margin:16px 0 4px}
.compose input,.compose textarea{width:100%;font-size:16px;
  border:1px solid var(--edge);border-radius:11px;background:var(--bg);
  color:var(--fg);font-family:inherit}
.compose input{min-height:var(--tap);padding:0 12px}
.compose textarea{min-height:9.5em;line-height:1.6;padding:12px;resize:vertical}
/* The two controls a browser draws itself if you let it: a subject picker and
   a file button. Left alone they arrive as a white system select with the OS
   chevron on it and a grey "Choose files / No file chosen" slab in a dashed
   box -- the two most dated pixels in the app, and the only two an eighteen
   year old has never seen in anything else they use.
   The select keeps its native wheel, which is still the fastest picker on a
   phone and costs nothing to download; what goes is the chrome around it.
   appearance:none drops the OS arrow, and the chevron below is the same '>'
   rotated a quarter turn that the club cards and the activity log already
   draw, so one glyph says "this opens" everywhere in the app. */
.compose select,.askbox select{width:100%;min-height:var(--tap);margin-top:16px;
  padding:0 40px 0 12px;font-size:16px;font-family:inherit;color:var(--fg);
  background:var(--bg);border:1px solid var(--edge);border-radius:11px;
  appearance:none;-webkit-appearance:none}
/* The papers screen. One subject at a time, picked from a strip that scrolls
   sideways; under it, a card per year and a tap target per paper. The strip
   bleeds to the screen edge so a half-shown chip says there are more. */
.pp-subs{display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;
  padding:4px 16px 12px;scroll-padding:0 16px;-webkit-overflow-scrolling:touch}
.pp-subs::-webkit-scrollbar{display:none}
.pp-sub{flex:none;min-height:38px;padding:0 14px;border-radius:999px;
  border:1px solid var(--line);background:transparent;color:var(--mut);
  font-family:inherit;font-size:13px;font-weight:500;white-space:nowrap}
.pp-sub[aria-selected=true]{background:var(--fg);border-color:var(--fg);color:var(--bg)}
.pp-head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;
  gap:12px;padding:8px 16px 16px}
.pp-head b{display:block;font-size:16px;font-weight:600;color:var(--fg)}
.pp-head small{display:block;margin-top:2px;font-size:13px;color:var(--mut)}
.seg{display:inline-flex;padding:3px;border-radius:11px;background:var(--surface)}
.seg button{min-height:32px;padding:0 12px;border:0;border-radius:7px;background:transparent;
  color:var(--mut);font-family:inherit;font-size:13px;font-weight:500}
.seg button[aria-pressed=true]{background:var(--bg);color:var(--fg);
  box-shadow:0 1px 2px rgba(0,0,0,.12)}
.pp-grid{display:grid;gap:12px;padding:0 16px 24px}
@media (min-width:900px){.pp-grid{grid-template-columns:1fr 1fr;align-items:start}}
.pp-year{border:1px solid var(--line);border-radius:14px;padding:14px 14px 6px}
.pp-year h3{margin:0 0 6px;font-size:13px;font-weight:600;color:var(--mut);
  font-variant-numeric:tabular-nums}
.pp-row{display:flex;gap:12px;align-items:flex-start;padding:6px 0 8px}
.pp-row+.pp-row{border-top:1px solid var(--line);padding-top:10px}
.pp-exam{flex:none;width:40px;padding-top:9px;font-size:13px;font-weight:500;color:var(--fg)}
.pp-chips{display:flex;flex-wrap:wrap;gap:6px;min-width:0}
.pp-chip{position:relative;display:inline-flex;align-items:center;justify-content:center;
  min-width:38px;height:36px;padding:0 10px;border-radius:7px;background:var(--surface);
  color:var(--fg);font-size:16px;font-weight:600;text-decoration:none;
  font-variant-numeric:tabular-nums}
.pp-chip.wide{font-weight:500;font-size:13px}
.pp-chip:active{transform:scale(.96)}
@media (hover:hover){.pp-chip:hover{background:color-mix(in srgb,var(--accent) 14%,var(--surface));
  color:var(--accent)}}
.pp-chip:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
/* Answers inside the PDF: a dot, and the chip's label says it in words. */
.pp-ans{position:absolute;top:5px;right:5px;width:6px;height:6px;border-radius:50%;
  background:var(--accent)}
.pick{position:relative;margin-top:16px}
.pick select{margin-top:0}
.pick::after{content:'\203a';position:absolute;right:15px;top:50%;margin-top:-11px;
  font-size:20px;line-height:1;color:var(--mut);transform:rotate(90deg);
  pointer-events:none}
/* The file button. appearance:none does nothing to an <input type=file> -- the
   slab is shadow DOM -- so the input is hidden and driven from a control that
   is built out of the same parts as every other control here. It stays in the
   DOM because it is what opens the picker and what holds the files; the same
   trick the Add sheet's own hidden input has always used. The line beside the
   button is what the browser's grey text was for: how many are picked. */
.shotpick{display:flex;align-items:center;gap:12px;margin-top:8px}
/* Scoped through .askbox, which sets every button in it to the full-width
   accent slab the Post button is. This one is not that button. */
.askbox .shotpick button{display:inline-flex;align-items:center;width:auto;flex:none;
  min-height:var(--tap);padding:0 16px;border-radius:11px;
  border:1px solid var(--edge);background:var(--bg);color:var(--accent);
  font-size:13px;font-weight:600}
.askbox .shotpick button:active{background:var(--surface);opacity:1}
.shotpick span{flex:1;min-width:0;font-size:13px;color:var(--mut)}
/* .quiet hangs off --hang, which is where a ROW's words begin. Inside a form
   the words begin at the edge of the controls. */
.askbox .quiet{padding:8px 0 0}
.compose .pinrow{display:flex;align-items:center;gap:11px;min-height:var(--tap);
  margin-top:14px;font-size:16px;color:var(--fg)}
.compose .pinrow input{width:22px;height:22px;min-height:0;flex:none;accent-color:var(--accent)}
.compose .pinrow label{margin:0;font-size:16px;color:var(--fg)}
.compose .err{margin:10px 0 0;font-size:13px;color:var(--err);min-height:1.2em}
.compose .go{display:flex;gap:8px;margin-top:6px}
.compose .go button{flex:1;min-height:var(--tap);border-radius:11px;background:var(--bg);
  border:1px solid var(--edge);font-size:16px;font-weight:600}
.compose .go button:active{background:var(--surface)}
.compose .go button.primary{background:var(--admin);color:var(--admin-fg);border-color:transparent}

/* Your own two fields, in the card they replace. */
.pform label{display:block;font-size:13px;color:var(--mut);margin:16px 0 4px}
.pform input{width:100%;min-height:var(--tap);font-size:16px;padding:0 12px;
  border:1px solid var(--edge);border-radius:11px;background:var(--bg);color:var(--fg)}
.pform .err{margin:10px 0 0;font-size:13px;color:var(--err);min-height:1.2em}
.pform .go{display:flex;gap:8px;margin-top:14px}
.pform .go button{flex:1;min-height:var(--tap);border-radius:11px;background:var(--bg);
  border:1px solid var(--edge);font-size:16px;font-weight:600}
.pform .go button:active{background:var(--surface)}
.pform .go button.primary{background:var(--accent);color:var(--accent-fg);border-color:transparent}

/* ---- Admin ink, the same treatment as the admin screen so the two read as
   one thing: an inked tick down the row, and the word on the end of it. */
.row.adm .tick{background:var(--admin)}

/* ---- Locked. A student sees the control, is told who it is for and what
   opens it, and never spends a request to find out. Muted on surface is
   5.2:1 light and 7.2:1 dark -- this is greyed, not unreadable. */
#fab.locked{background:var(--surface);color:var(--mut);border:1px solid var(--line);
  box-shadow:none}
#ask.locked{background:var(--surface);color:var(--mut);border:1px solid var(--line);
  box-shadow:0 6px 20px rgba(0,0,0,.18)}
#sheet .opt.locked{background:transparent;border-style:dashed;color:var(--mut)}
#sheet .opt.locked b{color:var(--mut)}

.rtop{display:flex;align-items:center;gap:6px}
.back{display:flex;align-items:center;gap:4px;height:var(--tap);padding:0 8px 0 4px;
  margin-left:-4px;font-size:16px;color:var(--accent);font-weight:500;flex:none}
/* Back says the subject by NAME. It used to say the code, next to a chip
   saying the same code -- the whole header spent on one string, twice, while
   the lecture's own title appeared nowhere on the screen at all. */
.rtop .back{display:block;min-width:0;max-width:calc(100% - 88px);
  line-height:var(--tap);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rtop .code{margin-left:auto}
/* The lecture's title, which is what this screen is. It lived in the row you
   tapped to get here and nowhere afterwards, so the top fifth of the reading
   screen was a back button, a chip, and 150px of nothing above the markdown's
   own first heading. */
.mast{max-width:70ch;margin:0 auto;padding:20px 18px 0}
.mast h1{margin:0;font-size:26px;line-height:1.2;letter-spacing:-.022em;font-weight:700}
.mast .meta{margin:6px 0 0;font-size:13px;color:var(--mut)}
/* Four sizes only -- 26/20/16/13, roughly a 1.25 step. h3 separates itself by
   weight rather than a fifth size that would read as body text. */
/* The dock's clearance moved to #doubts, which is the last thing on the
   reading screen now: 142px is #nav's, and the FAB reaches 76 + 58 = 134. */
article{padding:24px 18px 8px;max-width:70ch;margin:0 auto}
/* Under the masthead the note starts where the masthead left off. */
.mast+article{padding-top:20px}
.mast+article>:first-child{margin-top:0}
article h1{font-size:26px;line-height:1.2;letter-spacing:-.022em;font-weight:700;margin:0 0 28px}
article h2{font-size:20px;line-height:1.3;letter-spacing:-.015em;font-weight:700;
  margin:36px 0 12px}
/* The first thing under the title has no 36px of nothing above it. */
article h1+h2{margin-top:0}
/* Not the accent. The accent is what a link is, and every '###' in every note
   was set in it -- so a heading read as something to tap, and tapping it did
   nothing. Weight and the space above it are what make it a heading. */
article h3{font-size:16px;font-weight:700;color:var(--fg);margin:24px 0 4px}
article p{margin:0 0 16px}
/* The notes are written with "---" between sections, so the rule is theirs
   and not the page's. It is a hairline with air either side rather than the
   grooved 3D bar a browser draws by default; h2's own margin collapses into
   the one below it, so a section break is one gap and not two. */
article hr{border:0;border-top:1px solid var(--line);margin:32px 0}
/* Tailwind's preflight sets list-style:none on every ul and ol, which took
   the bullets off every list in every lecture note and left the indent behind
   -- so a key-points list read as four stranded paragraphs. Notes are the one
   place in this app where a list is prose and needs its markers back. */
article ul,article ol{padding-left:24px;margin:0 0 16px}
article ul,.ann .md ul{list-style:disc}
article ol,.ann .md ol{list-style:decimal}
article li{margin:4px 0}
article li::marker,.ann .md li::marker{color:var(--mut)}
article>:last-child{margin-bottom:0}
article code{background:var(--surface);padding:2px 5px;border-radius:7px;font-size:.92em}
article pre{background:var(--surface);padding:13px;border-radius:11px;overflow-x:auto;font-size:13px}
article img{max-width:100%;height:auto}
.katex-display{overflow-x:auto;overflow-y:hidden;padding:4px 0}
.scroll-x{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:12px 0}
table{border-collapse:collapse;font-size:16px;min-width:100%}
td,th{border:1px solid var(--line);padding:8px 12px;text-align:left}
th{background:var(--surface)}
/* A disclosure inside something you are READING: a worked step in a note, a
   thread on a file. Scoped away from .card, which is Campus's own <details>
   and lays its summary out as a name over a category -- left unscoped these
   five rules were centring every club in the directory and ruling a line under
   the open one -- and away from .nav-sect, which is a section of the map. A
   section of the map is one row in a column of rows: a filled slab with a
   ruled lid is a block of content, and five of them is not a navigation. */
details:not(.card):not(.nav-sect){margin:12px 0;background:var(--surface);border-radius:11px;overflow:hidden}
details:not(.card):not(.nav-sect)>summary{min-height:var(--tap);display:flex;align-items:center;
  padding:0 16px;color:var(--accent);font-size:16px;font-weight:600}
details:not(.card):not(.nav-sect)>summary:active{background:color-mix(in srgb,var(--fg) 7%,var(--surface))}
details:not(.card):not(.nav-sect)[open]>summary{border-bottom:1px solid var(--line)}
details:not(.card):not(.nav-sect)>:not(summary){padding:0 16px}
details:not(.card)>:not(summary):last-child{padding-bottom:4px}

.dock{
  position:fixed;left:0;right:0;bottom:0;z-index:6;display:flex;gap:8px;
  padding:9px 14px calc(9px + env(safe-area-inset-bottom));
  background:color-mix(in srgb,var(--bg) 88%,transparent);
  backdrop-filter:blur(12px);border-top:1px solid var(--line);
}
.dock button{
  flex:1;min-width:0;min-height:var(--tap);border-radius:11px;background:var(--surface);
  font-size:16px;font-weight:600;display:flex;align-items:center;justify-content:center;
}
.dock button.primary{background:var(--accent);color:var(--accent-fg)}
/* Share, Download and Print are things you might do to a note; Practice is the
   thing you opened it to do. They keep the full 44px target and give up the
   fill and the big size, so the dock reads as one action with four options
   beside it instead of five equal slabs -- and "Download" stops being clipped
   by its own pill at 375px. Only when Practice is actually there, though: on
   the two-pane layout the dock is drawn with no note open and no primary in
   it, and four bare words in a bar read as a bar that stopped working. */
.dock:has(button.primary:not([hidden])) button:not(.primary){
  background:none;color:var(--mut);font-size:13px}
/* Below .top's 5: it is placed in document coordinates, so a scroll can
   carry it into the sticky header, where it used to paint over the back
   button and eat the tap meant for it. */
#ask{position:absolute;z-index:4;display:none;padding:9px 15px;border-radius:11px;
  background:var(--accent);color:var(--accent-fg);font-size:16px;font-weight:600;
  box-shadow:0 6px 20px rgba(0,0,0,.28)}
#ask.on{display:block}
#panel{position:fixed;left:0;right:0;bottom:0;z-index:10;transform:translateY(101%);
  transition:transform .22s ease;background:var(--bg);border-top:1px solid var(--line);
  border-radius:18px 18px 0 0;max-height:76dvh;display:flex;flex-direction:column;
  box-shadow:0 -8px 34px rgba(0,0,0,.22)}
#panel.on{transform:none}
@media (prefers-reduced-motion:reduce){#panel{transition:none}}
#panel header{display:flex;align-items:center;gap:12px;padding:12px 16px;
  border-bottom:1px solid var(--line)}
#panel header b{font-size:16px;font-weight:600;flex:1}
#panel .quote{font-size:13px;color:var(--mut);padding:10px 16px 0;
  overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
#panel .out{padding:12px 16px calc(20px + env(safe-area-inset-bottom));overflow-y:auto;font-size:16px}
#panel .out .katex-display{overflow-x:auto}
#close{min-width:var(--tap);min-height:var(--tap);font-size:16px;color:var(--mut)}
#fab{position:fixed;right:16px;bottom:calc(76px + env(safe-area-inset-bottom));z-index:7;
  width:58px;height:58px;border-radius:50%;background:var(--accent);color:var(--accent-fg);
  font-size:30px;line-height:1;box-shadow:0 6px 22px rgba(0,0,0,.3)}
#fab:active{transform:scale(.94)}
/* And it steps out of the way while you are reading down a screen. Reserving
   the bottom of a list keeps the + off the LAST row; this is what keeps it
   off the forty rows above it, which is where it actually sat -- on a
   confession's third line, on a club card's buttons, on the middle of a
   paragraph. It goes on the way down and comes back the moment you stop or
   turn round, so it answers what somebody is doing rather than moving on its
   own, and it is never absent from a screen at rest. */
body.fabaway #fab{transform:translateY(96px);opacity:0;pointer-events:none}
/* A screen that already has a bar of its own does not get a second control
   floating over its words. Reading has the dock -- Practice, Save, Share,
   Download, Print -- and the + sat 76px above it, in the middle of the
   paragraph you were reading. It comes straight back with the tab bar the
   moment the note is closed, and on the wide layout it never goes at all:
   there the note is one column and the list is still the other, so the +
   belongs to the list and is nowhere near the words. */
body.reading #fab{display:none}
#ask:active{opacity:.8}
/* #ask is clamped to innerWidth-130, which on a 390px screen puts its right
   edge inside the FAB's band -- and the FAB is z-index 7 above #ask's 4, so a
   tap there opened the Add sheet instead. They are never both wanted. */
body:has(#ask.on) #fab{display:none}
#sheet{position:fixed;inset:0;z-index:11;display:none;background:rgba(0,0,0,.45)}
/* The sheet comes up from the edge it is anchored to rather than appearing on
   top of the screen, which is the one motion that says where a thing came
   from and therefore where Cancel puts it back. */
#sheet.on{display:block;animation:fadein 140ms ease-out}
#sheet.on .card{animation:riseup 180ms ease-out}
#sheet .card{position:absolute;left:0;right:0;bottom:0;background:var(--bg);
  border-radius:18px 18px 0 0;padding:18px 16px calc(18px + env(safe-area-inset-bottom));
  max-height:88dvh;overflow-y:auto}
#sheet h3{margin:0 0 16px;font-size:20px;font-weight:700;letter-spacing:-.015em}
#sheet label{display:block;font-size:13px;color:var(--mut);margin:16px 0 8px}
#sheet select,#sheet .opt{width:100%;min-height:var(--tap);font-size:16px;border-radius:11px;
  border:1px solid var(--edge);background:var(--bg);color:var(--fg);padding:0 12px}
#sheet .opt{display:flex;flex-wrap:wrap;align-items:center;gap:0 12px;
  padding:8px 12px;margin-top:8px;text-align:left;line-height:1.4}
#sheet .opt:active{background:var(--surface)}
#sheet .opt b{font-weight:600}
#sheet .opt span{flex:1 0 100%;color:var(--mut);font-size:13px}
#rec .time{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
#rec .dot{display:inline-block;width:11px;height:11px;border-radius:50%;background:var(--err);
  margin-right:8px;animation:pulse 1.4s infinite}
@keyframes pulse{50%{opacity:.25}}
@media (prefers-reduced-motion:reduce){#rec .dot{animation:none}}
#prog{margin-top:16px;display:none}
#prog.on{display:block}
/* Only appears when more than one file was picked at once -- a single file
   auto-uploads exactly as it always has, named after itself. */
#batchName label{display:block;font-size:13px;color:var(--mut);margin-bottom:8px}
#batchName input{width:100%;height:var(--tap);padding:0 12px;font:inherit;
  border:1px solid var(--edge);border-radius:11px;background:var(--bg);color:var(--fg)}
#batchName .go{display:flex;gap:10px;margin-top:12px}
#batchName .go button{flex:1;min-height:var(--tap);border-radius:11px;font:inherit;
  font-weight:600;border:1px solid var(--edge);background:transparent;color:var(--fg)}
#batchName .go button:active{background:var(--surface)}
#batchName .go button.primary{background:var(--accent);color:var(--accent-fg);border:0}
#prog .bar{height:10px;border-radius:5px;background:var(--surface);overflow:hidden}
#prog .fill{height:100%;width:0;background:var(--accent);transition:width .18s linear}
#prog .txt{margin-top:9px;font-size:13px;color:var(--mut);text-align:center}
#prog.err .fill{background:var(--err)}
.job .bar{height:4px;border-radius:2px;background:var(--line);margin-top:6px;overflow:hidden}
.job .bar i{display:block;height:100%;background:var(--accent);width:0;transition:width .3s}
.job .col{flex:1;min-width:0}
/* A disclosure, drawn as one. It is the first thing on the You screen and as
   a bare left-aligned sentence it read as something left behind rather than
   something to press. */
#tools{margin-top:8px}
#logbtn{display:flex;align-items:center;width:100%;min-height:var(--tap);
  padding:0 12px;border-radius:11px;font-size:13px;font-weight:600;
  color:var(--mut);text-align:left}
#logbtn::after{content:'\203a';margin-left:auto;padding-left:8px;font-size:16px;
  line-height:1;transform:rotate(90deg);transition:transform 140ms ease-out}
#logbtn[aria-expanded="true"]::after{transform:rotate(270deg)}
#logbtn:active{background:var(--surface)}
#logbox{display:none;margin-top:8px;padding:12px;border-radius:11px;background:var(--surface);
  font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre;
  overflow-x:auto;max-height:44vh;overflow-y:auto}
#logbox.on{display:block}
#jobs{padding:0 16px}
.job .st{font-size:13px;color:var(--mut);margin-left:auto;text-align:right}
.job.failed{box-shadow:inset 3px 0 0 var(--err)}
.job.failed .st{color:var(--err);font-weight:600}
.spin{width:14px;height:14px;border:2px solid var(--line);border-top-color:var(--accent);
  border-radius:50%;animation:spin .8s linear infinite;flex:none}
@keyframes spin{to{transform:rotate(360deg)}}
/* One indeterminate indicator for anything slow. It goes in the strip below
   for page-level work and straight into the Explain panel for a doubt, so a
   new slow call gets a progress state by calling waiting() and nothing else. */
.wait{height:4px;border-radius:2px;background:var(--line);overflow:hidden}
.wait i{display:block;height:100%;width:38%;border-radius:2px;background:var(--accent);
  animation:slide 1.15s ease-in-out infinite}
@keyframes slide{0%{transform:translateX(-105%)}100%{transform:translateX(275%)}}
.waitmsg{margin:8px 0 0;font-size:13px;color:var(--mut)}
/* A whole section that has not come back yet. Same bar as every other slow
   thing, set where that section's rows will be, so the screen says "still
   coming" in the place the answer will appear. A line of grey text alone
   cannot say that: it is the same shape as a section with nothing in it. */
.waitline{padding:12px 16px 8px var(--hang)}
/* Anchored at the bottom, clear of the FAB (76 + 58) and the dock, and never
   takes a tap. It used to float over the header, where it covered the back
   button and the search box and ate the taps meant for them. */
#busy{position:fixed;z-index:6;display:none;pointer-events:none;left:12px;right:12px;
  bottom:calc(144px + env(safe-area-inset-bottom));max-width:34rem;margin:0 auto;
  padding:13px 15px;border-radius:11px;background:var(--surface);
  border:1px solid var(--line);box-shadow:0 8px 26px rgba(0,0,0,.22)}
#busy.on{display:block}
.dock button:active{opacity:.7;transform:scale(.98)}
body:not(.reading) .dock{display:none}

/* ---- The shell: four tabs, thumb-reachable, always there except while
   reading -- where the dock takes the same strip and back brings them
   straight back. Labels, not icons: no webfont to download, and eight
   readable characters beat a glyph nobody has been taught. */
.tabs{
  position:fixed;left:0;right:0;bottom:0;z-index:6;display:flex;gap:4px;
  padding:6px 8px calc(6px + env(safe-area-inset-bottom));
  background:color-mix(in srgb,var(--bg) 88%,transparent);
  backdrop-filter:blur(12px);border-top:1px solid var(--line);
}
.tabs button{
  flex:1;min-height:var(--tap);border-radius:11px;font-size:11px;font-weight:500;
  color:var(--mut);display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:3px;padding:5px 0 4px;
}
.tabs button svg{width:23px;height:23px}
.tabs button[aria-current]{color:var(--accent);font-weight:600}
.tabs button:active{background:var(--surface);transform:scale(.97)}
body.reading .tabs{display:none}
/* Clear of the bar AND of the FAB above it (76 + 58), so the last row is never
   half under either. 80px cleared only the bar, and the FAB then sat on top of
   the last row's vote button with no scroll left to escape it. */
#nav{padding-bottom:var(--fabclear)}

/* ---- Home: a plain line of prose where a row would lie. -------------- */
.quiet{padding:8px 16px 8px var(--hang);margin:0;font-size:13px;color:var(--mut)}

/* ---- The timetable editor. One day at a time, eight native selects: the
   iOS wheel is the fastest subject picker on a phone and it costs nothing to
   download. Six days of tapping is under two minutes, which is the whole
   design brief for this screen. */

/* ---- Practice: one question at a time, over everything else. ---------- */
#quiz{position:fixed;inset:0;z-index:12;background:var(--bg);display:flex;flex-direction:column}
.qtop{display:flex;align-items:center;gap:12px;flex:none;
  padding:max(10px,env(safe-area-inset-top)) 16px 10px;border-bottom:1px solid var(--line)}
.qtop b{flex:1;font-size:16px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#qcount{font-size:13px;color:var(--mut);font-variant-numeric:tabular-nums;flex:none}
#qexit{min-width:var(--tap);min-height:var(--tap);font-size:16px;color:var(--mut);flex:none}
#qexit:active{background:var(--surface);border-radius:11px}
#qbar{flex:none;height:4px;background:var(--line)}
#qbar i{display:block;height:100%;width:0;background:var(--accent);transition:width .2s}
#qmain{flex:1;overflow-y:auto;width:100%;max-width:70ch;margin:0 auto;
  padding:24px 18px 28px;display:flex;flex-direction:column;
  /* "safe" so a question taller than the screen still starts at the top:
     centred overflow puts the first line above the scroll origin, where it
     cannot be reached. A browser that does not know the keyword drops the
     declaration and lands on flex-start, which is the same outcome. */
  justify-content:safe center}
#qmain:has(#qa>*){justify-content:flex-start}
#qsrc{margin:0 0 12px;font-size:13px;color:var(--mut);flex:none}
#qq{font-size:20px;line-height:1.4;font-weight:600}
#qq p{margin:0 0 10px}
#qq .katex-display{font-weight:400}
/* The one number a finished run is for, on a screen with nothing else on it.
   Size and gradient are set with the other display type at the top of this
   block; what is left here is the space under it. */
#qscore{margin:0 0 10px}
#qsub{margin:0;color:var(--mut)}
#qa{margin-top:24px;padding-top:20px;font-size:16px}
#qa:has(>*){border-top:1px solid var(--line)}
#qa>*{animation:arrive 180ms ease-out}
#qa>:first-child{margin-top:0}
.qdock{flex:none;display:flex;gap:8px;padding:9px 14px calc(9px + env(safe-area-inset-bottom));
  border-top:1px solid var(--line)}
/* Practice makes the reading dock four buttons wide on a phone. */
.dock button{white-space:nowrap}

@media (min-width:760px){
  body{display:flex}
  /* Two panes and not three. A 320px column of subjects parked between the
     map and the note was a third piece of furniture doing the job the second
     one already does: it IS the screen you are on. So the wide layout is the
     phone's -- one content pane that the map points at and a note replaces --
     with the map standing open beside it instead of sliding over it.
     The pane does not stretch to 1100px because a row of six words does not
     get better at that width; it is held to --colw and centred, and the map
     is the only thing pinned to an edge. */
  #list,#read{flex:1;min-width:0}
  #list>*{max-width:var(--colw);margin-left:auto;margin-right:auto}
  article{max-width:70ch;margin:0 auto;padding:32px 40px 110px}
  /* The dock belongs to the note, so it stops where the note's column stops
     rather than stretching five buttons across 960px of empty pane. */
  .dock{left:var(--railw);justify-content:center;padding-left:40px;padding-right:40px}
  .dock button{flex:0 1 9rem}
  /* A note covers the list it came out of, exactly as it does on a phone, so
     the way back out of it is a button again and not the other pane. It is
     held to the note's own column so that Back starts where the title does
     rather than stranded at the far left of a 1100px bar. */
  .rtop{max-width:calc(70ch + 80px);margin:0 auto;padding-left:40px;padding-right:40px}
  .mast{max-width:70ch;margin:0 auto;padding:32px 40px 0}
  .mast+article{padding-top:16px}
  /* No thumb bar down here to clear, so the + sits where a floating control
     sits on a desktop: in the corner. It still steps out while a note is
     open, because the note has the dock. */
  #fab{right:24px;left:auto;bottom:24px}
  /* Five buttons that act on nothing are worse than no bar: Save, Share,
     Download and Print with no note open were four live-looking controls
     over an empty pane. The dock belongs to the note and arrives with it. */
  body:not(.reading) .dock{display:none}
  /* The drawer stops being a drawer. There is room for the map to stand open
     beside the two panes, so it does -- and the bottom bar goes, because a
     thumb bar pinned under a 320px column on a 1400px screen is a phone
     control that came along by accident. Nothing is lost: every tab in it is
     a row in the rail, under the group it belongs to.
     Same element, so there is one list of destinations in this app and not a
     wide one and a narrow one that drift apart. */
  #drawer{position:sticky;top:0;left:auto;bottom:auto;height:100dvh;
    width:var(--railw);flex:none;transform:none;visibility:visible;box-shadow:none;
    transition:none;padding-bottom:16px}
  #scrim,#dclose{display:none}
  .dhead{padding:26px 16px 18px}
  /* The name is in the rail, two inches to the left and on every screen. A
     second "recarve" at the top of the list column was the app introducing
     itself to somebody already inside it. */
  .brand{display:none!important}
  /* With the name gone and no screen name on Home, this strip has nothing in
     it -- and an empty 44px band above the search box is furniture. */
  .tophead{min-height:0;margin-bottom:0}
  .tophead:has(.shead:not([hidden])){margin-bottom:4px}
  /* The face opened the drawer; with the rail already open it has nothing to
     open, and "Your profile" is a row in the rail two inches to the left. */
  #avatar{display:none}
  .tabs{display:none}
  /* The screen's name is the biggest type in the app on a phone, where it has
     the whole width. Here it has a 320px column and the note beside it is the
     headline on this layout, so the name steps back one place on the scale --
     far enough not to compete with the reading column, still the largest thing
     in the column it names. */
  .shead h2,.brand b{font-size:26px;letter-spacing:-.022em;line-height:1.15}
}
/* ---- Campus: societies, what they are running, and where anything is. ----
   Cards rather than rows, because each of these carries more than two lines
   and a row that wraps to four is a row pretending to be a card. */
.card{border-radius:14px;background:var(--surface);
  padding:12px 16px;margin:0 16px 8px}
.card+.card{margin-top:0}
.card h3{margin:0;font-size:16px;font-weight:600;color:var(--fg)}
.card .meta{display:block;font-size:13px;color:var(--mut);margin-top:3px}
.card p{margin:8px 0 0;font-size:13px;line-height:1.55;color:var(--fg)}
.card .tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}
.card .tags span{font-size:11px;color:var(--mut);border:1px solid var(--line);
  border-radius:999px;padding:3px 9px;background:var(--bg)}
.card .acts{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.card .acts a,.card .acts button{min-height:var(--tap);display:inline-flex;
  align-items:center;padding:0 16px;border-radius:11px;border:1px solid var(--edge);
  background:var(--bg);color:var(--fg);font-size:13px;font-weight:600;
  text-decoration:none}
.card .acts a:active,.card .acts button:active{background:var(--surface)}
.card.gone{opacity:.6}
/* The date, torn off, so an event reads as a date first and a name second --
   the same tile Home's Coming up list already uses. */
.card .when{font-size:13px;color:var(--mut);font-weight:600}
/* A club opens in place. <details> is the platform's own disclosure: it works
   with no JavaScript, it is keyboard and screen-reader correct already, and it
   needs no URL of its own for something that is two sentences long. */
.card summary{list-style:none;cursor:pointer;position:relative;
  display:flex;flex-direction:column;justify-content:center;
  min-height:var(--tap);padding-right:24px}
/* A society's mark beside its two lines. Only the summary that has one turns
   into a row: an event leads with a torn-off date and a place with its name,
   and neither of those is somebody to draw. */
.card summary:has(.face){flex-direction:row;align-items:center;gap:12px;
  justify-content:flex-start}
.card summary .nm{display:flex;flex-direction:column;justify-content:center;
  min-width:0}
.card summary::-webkit-details-marker{display:none}
.card summary::after{content:'›';position:absolute;right:0;top:50%;
  margin-top:-10px;width:20px;text-align:center;color:var(--mut);font-size:20px;
  line-height:1;transform:rotate(90deg);transition:transform 140ms ease-out}
.card[open] summary::after{transform:rotate(270deg)}
.card summary:active{opacity:.7}
/* The map, when there is a key. Square-ish and bounded; with no key this
   element is never created at all and the places list stands on its own. */
#campusmap{height:260px;margin:0 16px 8px;border-radius:14px;overflow:hidden;
  border:1px solid var(--line);background:var(--surface)}
@media print{
  .top,.dock,.tabs,#list,.rtop,#fab,#busy,#ask,#quiz,#drawer,#scrim{display:none!important}
  #read{display:block!important}
  article{padding:0;max-width:none}
  details{background:none;border:1px solid #999}
}
</style>
<!-- Tailwind's compiled sheet, inlined. It comes AFTER the token block above
     because these utilities are written in terms of those variables, and
     because the token block has to stay the page's first <style>. -->
<style>__CSS__</style>

<!-- The map. One element, two shapes: a drawer over the page on a phone and a
     rail beside it on a wide screen. It is first in the document so that a
     keyboard and a screen reader meet the navigation before the screen it
     navigates; closed, it is visibility:hidden and therefore in neither's
     way. -->
<div id="scrim"></div>
<nav id="drawer" aria-label="Everywhere in recarve">
  <div class="dhead">
    <span class="who"><b>recarve</b><small>Section I</small></span>
    <button id="dclose" aria-label="Close the menu">Close</button>
  </div>
  <div id="dnav"></div>
  <!-- Who you are, at the foot of the map, where an account has sat in every
       app anybody here has already used. The four rows that used to be a "You"
       section halfway down the list open out of the face itself, and the one
       appearance control stands beside it rather than under a heading of its
       own. -->
  <div class="foot">
    <div id="dyou"></div>
    <button id="themebtn" aria-label="Switch to dark"></button>
  </div>
</nav>

<section id="list">
  <div class="top">
    <button class="back" id="lback" aria-label="Back to all subjects">&lsaquo; Subjects</button>
    <div class="tophead">
      <div class="brand" id="brand"><b>recarve</b><span>Section I</span></div>
      <div class="shead" id="shead" hidden>
        <span class="code" id="scode"></span>
        <h2 id="sname"></h2>
      </div>
      <button id="avatar" aria-expanded="false" aria-controls="drawer"
              aria-label="Everywhere in recarve"><svg viewBox="0 0 24 24" aria-hidden="true"
        fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
        ><circle cx="12" cy="8.6" r="3.7"/><path d="M4.9 20c.8-3.7 3.7-5.8 7.1-5.8s6.3 2.1 7.1 5.8"/></svg></button>
    </div>
    <input id="q" placeholder="Search notes and transcripts" autocomplete="off" enterkeyhint="search">
  </div>
  <div id="jobs"></div>
  <div id="tools" style="padding:0 16px" hidden>
    <button id="logbtn" aria-expanded="false" aria-controls="logbox">Show activity log</button>
    <pre id="logbox"></pre>
  </div>
  <nav id="nav"></nav>
</section>

<section id="read">
  <div class="top rtop">
    <button class="back" id="back" aria-label="Back to the subject">&lsaquo; Back</button>
    <span class="code" id="rcode"></span>
  </div>
  <header class="mast" id="mast" hidden>
    <h1 id="rtitle"></h1>
    <p class="meta" id="rmeta"></p>
  </header>
  <article id="body"><p class="blank">Pick a lecture to start reading.</p></article>
  <section id="doubts" class="thread" aria-label="Doubts"></section>
</section>

<button id="fab" aria-label="Add a lecture or notes">+</button>

<div id="busy" role="status" aria-live="polite"></div>

<div id="sheet">
  <div class="card">
    <h3>Add to the library</h3>
    <label for="subj">Subject</label>
    <select id="subj"></select>
    <div id="lock" hidden>
      <b>Adding is for trusted members</b>
      An admin makes you one. Ask in your class group and say what you want
      to add. Until then everything here is still yours to read, search,
      practise from and upvote.
    </div>
    <button class="opt" id="opt-rec"><b>Record this class</b><span>keep the screen on</span></button>
    <button class="opt" id="opt-audio"><b>Upload a recording</b><span>m4a, mp3, mp4 &middot; up to __AUDIO_MB__ MB</span></button>
    <button class="opt" id="opt-doc"><b>Upload notes, slides or photos</b><span>pdf, txt, md, photos of pages &middot; up to __DOC_MB__ MB each &middot; pick more than one to name them together</span></button>
    <button class="opt" id="opt-revise"><b>Make a revision sheet</b><span>from every lecture in this subject</span></button>
    <div id="batchName">
      <label for="btitle" id="blabel">One title for all these files</label>
      <input id="btitle" placeholder="Unit 3 handout" maxlength="200" autocomplete="off">
      <div class="go"><button id="bcancel">Cancel</button>
        <button id="bgo" class="primary">Upload</button></div>
    </div>
    <div id="prog">
      <div class="bar"><div class="fill" id="fill"></div></div>
      <p class="txt" id="ptxt">Uploading…</p>
    </div>
    <div id="rec">
      <div class="time"><span class="dot"></span><span id="clock">0:00</span></div>
      <button class="opt" id="opt-stop" style="justify-content:center"><b>Stop and upload</b></button>
    </div>
    <button class="opt" id="opt-close" style="justify-content:center;margin-top:16px">Cancel</button>
    <input type="file" id="file" accept="audio/*,video/*,.pdf,.txt,.md" hidden>
  </div>
</div>

<button id="ask">Explain</button>

<div id="panel" role="dialog" aria-label="Explanation">
  <header><b>Explain</b><button id="close" aria-label="Close">Close</button></header>
  <p class="quote" id="quote"></p>
  <div class="out" id="out"></div>
</div>

<div id="quiz" role="dialog" aria-label="Practice questions" hidden>
  <div class="qtop">
    <button id="qexit" aria-label="Close practice">&lsaquo;</button>
    <b id="qtitle"></b>
    <span id="qcount"></span>
  </div>
  <div id="qbar"><i></i></div>
  <div id="qmain" aria-live="polite">
    <p id="qsrc"></p>
    <div id="qq"></div>
    <div id="qa" hidden></div>
  </div>
  <div class="qdock">
    <button id="qshow" class="primary">Show the answer</button>
    <button id="qright" class="primary" hidden>I got it</button>
    <button id="qwrong" hidden>I got it wrong</button>
    <button id="qretry" class="primary" hidden>Try the missed ones</button>
    <button id="qagain" hidden>Start over</button>
  </div>
</div>

<div class="dock">
  <button id="practice" class="primary" hidden>Practice</button>
  <button id="save" aria-pressed="false">Save</button>
  <button id="share">Share</button>
  <button id="dl">Download</button>
  <button id="print">Print</button>
</div>

<nav class="tabs" id="tabs" aria-label="Sections">
  <button data-tab="home"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none"
    stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"
    ><path d="M3.8 10.4 12 4.2l8.2 6.2V20H3.8z"/><path d="M9.6 20v-5.4h4.8V20"/></svg
    ><span>Home</span></button>
  <button data-tab="classes"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none"
    stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"
    ><path d="M12 7.3C10.6 6.1 8.7 5.5 4.8 5.5v12.2c3.9 0 5.8.6 7.2 1.8 1.4-1.2 3.3-1.8 7.2-1.8V5.5c-3.9 0-5.8.6-7.2 1.8z"/><path d="M12 7.3v12.2"/></svg
    ><span>Classes</span></button>
  <button data-tab="campus"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none"
    stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"
    ><path d="M12 20.8s6.4-5.9 6.4-10.1a6.4 6.4 0 1 0-12.8 0C5.6 14.9 12 20.8 12 20.8z"/><circle cx="12" cy="10.4" r="2.3"/></svg
    ><span>Campus</span></button>
  <button data-tab="community"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none"
    stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"
    ><circle cx="9" cy="8.6" r="3.3"/><path d="M2.9 19.6c.7-3.4 3.1-5.4 6.1-5.4s5.4 2 6.1 5.4"/><path d="M16.1 5.7a3.3 3.3 0 0 1 0 5.8M17 14.6c2.1.6 3.4 2.4 3.8 5"/></svg
    ><span>Community</span></button>
</nav>

<script>
const DATA = __DATA__;
const nav = document.getElementById('nav'), body = document.getElementById('body');
const backBtn = document.getElementById('back');
const q = document.getElementById('q'), rcode = document.getElementById('rcode');
const mast = document.getElementById('mast');
const rtitle = document.getElementById('rtitle'), rmeta = document.getElementById('rmeta');
const brand = document.getElementById('brand'), shead = document.getElementById('shead');
const scode = document.getElementById('scode'), sname = document.getElementById('sname');
const lback = document.getElementById('lback'), tools = document.getElementById('tools');
const tabBtns = document.querySelectorAll('.tabs button');
const avatarEl = document.getElementById('avatar');
const drawerEl = document.getElementById('drawer');
const dnav = document.getElementById('dnav');
const dyou = document.getElementById('dyou');
// Declared up here with the other handles rather than beside drawMyFace:
// paintDrawer reads it, and paintDrawer runs on the first render.
let myName = null;
let current = null;                      // the note being read, or null
let currentCode = null;                  // its subject's code, alongside it
// Which tab, and how deep inside it. Classes goes subject -> note; Home has
// one level under it, the timetable editor.
let view = {tab: 'home', code: null, title: null, edit: false, att: false,
            sec: null};

// What the server told us last, held so Home can draw itself without asking
// for anything. All three arrive on the /data the page already fetches.
let TT = null;      // this student's timetable; null until the server answers
let PENDING = 0;    // classmates waiting for an admin; 0 for everyone else
// student reads, trusted also adds, admin also runs the class. The server is
// the one that enforces it -- hiding a button is a courtesy, not a lock, and
// /upload and /explain refuse a student whatever this page shows. A static
// export has no server and nobody to be, so it assumes the role that leaves
// every button working and lets each one say what it needs.
let ROLE = null;    // null until /data answers; see refresh() for no server at all
let JOBS = [];      // the last /jobs answer

// The ladder, in the same order as ROLES in notes.py -- change it there and
// change it here, and the test that reads both is what says so. Every question
// this page asks about a role goes through atLeast(), never through `===`: a
// screen that names the roles which may do a thing is a screen somebody has to
// remember to edit the day a role is added, and nobody does.
//
// Unknown is not a role. ROLE is null until /data answers and indexOf says -1
// to that, so every control waits rather than appearing and then locking.
const ROLES = ['student', 'trusted', 'cr', 'admin'];
const atLeast = r => ROLES.indexOf(ROLE) >= ROLES.indexOf(r);

// Hue per department prefix. Colour says which subject you are in, so the code
// chip reads at a glance without parsing the number.
const HUES = {MC:245, CY:150, EE:38, ME:210, BS:175, HS:345, SA:275, NC:80};
const hue = code => HUES[code.slice(0, 2)] ?? 220;

// ---- Four tabs, and inside Classes three levels: subjects -> one subject
// ---- -> one note.
// Every tab and every level is a real URL and each step is a pushState, so the
// Android back gesture and the browser back button both climb one step rather
// than leaving the page. Nothing keeps its own back stack: route() reads the
// hash, and the hash is the only thing that decides what is on screen.
// Four of them are in the bar. The fifth -- you -- is reached from the avatar
// in the header instead, because your own profile is not somewhere you go
// between classes the way the other four are. It stays a tab as far as the
// router is concerned, which is what keeps every '#me' link ever sent working.
const TABS = ['home', 'classes', 'campus', 'community', 'me'];
const TAB_TITLE = {classes: 'Subjects', campus: 'Campus', community: 'Community',
                   me: 'You'};
// Levels that changed tabs when each thing was given exactly one home. A link
// sent before the move still opens the screen it named: route() rewrites the
// hash in place and runs again, which is what the pre-tabs note links below
// already did.
// What is INSIDE Campus and Community. Each of these six was a heading you
// had to scroll to and nothing else -- no URL, no way in from the map, and on
// Campus you passed What's on and Clubs to reach the map every time. They are
// levels now, the same way the day view and the timetable are levels inside
// Classes: the tab with none of them named still shows all three, which is
// what the thumb bar goes to.
// None of these words may collide with a composer's -- COMPOSERS is event,
// club and place, WALL_COMPOSE is say and confess -- because both live at the
// same step of the hash.
const SECTIONS = {
  campus:    ['events', 'clubs', 'places'],
  community: ['board', 'doubts', 'standings'],
};
const sectionOf = (tab, word) =>
  (SECTIONS[tab] || []).includes(word) ? word : null;

const MOVED = {
  // The catch-up screen is Classes now -- it is about the week, and the week
  // is where the subjects are.
  'home/attendance': 'classes/attendance',
  // The timetable editor is gone: a section's week is the registrar's grid,
  // set once by an admin, and a student editing their own copy of it only
  // ever drifted away from the real one. Both old links land on Subjects,
  // which is the screen that week is now read from.
  'home/timetable': 'classes', 'classes/timetable': 'classes',
  // The wall and the class board are Community: they are people, not place.
  'campus/feed': 'community', 'campus/confession': 'community',
  'campus/board': 'community',
  // And the notice board is Home, so writing one starts from Home.
  'campus/new': 'home/new',
};
const subjectOf = code => DATA.find(s => s.code === code);
const lecturesOf = s => s.notes.filter(n => n.kind !== 'revision');
const revisionOf = s => s.notes.find(n => n.kind === 'revision');
const plural = (n, word) => n + ' ' + word + (n === 1 ? '' : 's');

function counts(s) {
  const bits = [];
  if (lecturesOf(s).length) bits.push(plural(lecturesOf(s).length, 'lecture'));
  if (s.uploads.length) bits.push(plural(s.uploads.length, 'note'));
  if (revisionOf(s)) bits.push('revision sheet');
  if (PAPER_COUNTS[s.code]) bits.push(plural(PAPER_COUNTS[s.code], 'paper'));
  return bits.join(' \u00b7 ') || 'Nothing yet';
}

const hashOf = (...parts) =>
  '#' + parts.filter(Boolean).map(encodeURIComponent).join('/');

function go(...parts) {
  const h = hashOf(...parts);
  // Tapping the tab you are already on must not stack history entries.
  if (h !== location.hash) history.pushState(null, '', h);
  route();
}

// A destructive action, and this app has no native confirm() dialog anywhere
// -- one drawn by the OS would be the one thing on this screen that does not
// match the room around it. The first tap arms it and says so in the same
// red the error text uses; the SAME tap fired again is the confirmation.
// Armed resets on its own the moment the row it belongs to is redrawn, which
// is the normal case: nothing here holds a timer to un-arm it.
// Admin-only, next to Remove. Tapping it turns the whole row into an edit
// box holding the current name; Save sends it, Cancel (or saving the same
// name) just redraws. The server keeps a file's extension whatever is typed.
function renameBtn(current, save) {
  const b = document.createElement('button');
  b.className = 'row-del';
  b.textContent = 'Rename';
  b.onclick = (e) => {
    e.stopPropagation();
    const row = b.parentNode;
    const form = document.createElement('form');
    form.className = 'rename';
    const input = document.createElement('input');
    input.value = current;
    input.maxLength = 120;
    input.setAttribute('aria-label', 'New name');
    const ok = document.createElement('button');
    ok.className = 'primary'; ok.textContent = 'Save';
    const no = document.createElement('button');
    no.type = 'button'; no.textContent = 'Cancel';
    no.onclick = () => render();
    form.onsubmit = async (ev) => {
      ev.preventDefault();
      const v = input.value.trim();
      if (!v || v === current) return render();
      ok.disabled = true;
      await save(v, ok);
    };
    form.append(input, ok, no);
    row.innerHTML = '';
    row.appendChild(form);
    input.focus();
    input.select();
  };
  return b;
}

// One POST for all three shapes, then a fresh /data so the shelf, the ranking
// and any open search all see the new name at once.
async function renameItem(payload, btn) {
  try {
    const r = await fetch('/rename', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || 'could not rename that');
    await refresh();
  } catch (e) {
    btn.disabled = false;
    busyDone(e.message);
  }
}

function removeBtn(onConfirmed) {
  const b = document.createElement('button');
  b.className = 'row-del';
  b.textContent = 'Remove';
  let armed = false;
  b.onclick = async (e) => {
    e.stopPropagation();
    if (!armed) {
      armed = true;
      b.classList.add('armed');
      b.textContent = 'Tap again to remove';
      return;
    }
    b.disabled = true;
    await onConfirmed(b);
  };
  return b;
}

function noteRow(n, s) {
  // A row that carries its own button cannot itself be one -- same reason
  // fileRow's root has always been a div. Only admin ever adds a second
  // control, so only admin pays for the extra element.
  const admin = atLeast('admin');
  const el = document.createElement(admin ? 'div' : 'button');
  el.className = 'row';
  el.style.setProperty('--h', hue(s.code));
  el.innerHTML = '<i class="tick"></i>'
    + (admin ? '<button class="name">' : '<span class="name">')
    + '<b></b><small></small>' + (admin ? '</button>' : '</span>');
  el.querySelector('b').textContent = n.title;
  // Blank until the server says who: the static export has no database behind
  // it, and "recorded by nobody" would be a worse answer than silence.
  el.querySelector('small').textContent = n.by ? 'recorded by ' + n.by : '';
  const open = () => go('classes', s.code, n.title);
  if (admin) el.querySelector('button.name').onclick = open; else el.onclick = open;
  // Revision sheets have no row in `lectures` at all -- Revise regenerates one
  // on the next tap, so there is nothing here an irreversible delete is for.
  if (admin && n.kind === 'lecture') {
    el.appendChild(renameBtn(n.title, (v, btn) =>
      renameItem({kind: 'lecture', subject: s.code, title: n.title, name: v}, btn)));
    el.appendChild(removeBtn(async (btn) => {
      try {
        const r = await fetch('/remove-lecture', {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({subject: s.code, title: n.title})});
        if (!r.ok) throw new Error(
          (await r.json().catch(() => ({}))).error || 'could not remove that');
        await refresh();
      } catch (e) {
        btn.disabled = false; btn.classList.remove('armed'); btn.textContent = 'Remove';
        busyDone(e.message);
      }
    }));
  }
  return el;
}

// ---- A face, wherever a person or a society is named. --------------------
// One helper, six screens. Initials in a circle, and the hue of the circle is
// read off the name itself, so the same person is the same colour on the feed,
// in a thread, on the board and in the header -- and a list of twenty rows
// stops being twenty rows that begin at the same x in the same grey.
//
// No image, no file, no letter that is not already on the screen beside it:
// the two colours are the code chip's own, --chip-lum behind and --chip-text
// on top, a pair that clears 4.5:1 at every one of the 360 hues in both
// themes. Colour per name was already this app's idea; this is the same idea
// applied to people.
//
// ANONYMOUS IS NOT A FACE. A confession carries no author at all -- `by` is
// null for every one of them because `authenticated` has no select privilege
// on posts.author_id (0035) -- and face(null) is the branch that draws that:
// one empty ring, no letter, no hue, identical on every confession. Nothing
// about it is derived from the row, because anything derived from the row is
// a fingerprint even when it is not a name.
// Knuth's multiplier rather than the usual 31, and the wrap left until the
// end: taking the remainder at every step throws away everything but the last
// couple of letters, which is how the eight names on the seeded board came out
// three degrees apart. Measured on the real board and the real twenty-four
// societies -- this is the one that puts no two names next to each other in
// the same colour.
const nameHue = name => {
  let n = 0;
  for (const ch of name) n = (Math.imul(n, 2654435761) + ch.codePointAt(0)) | 0;
  return Math.abs(n) % 360;
};

// The first letter of the first two words, or the first two letters when
// there is only one -- which is how "Sneha Bhattacharya" and "Evolve" are
// both shortened out loud. Stepped by code point rather than by index: a name
// here is as likely to be written in Devanagari as in Latin, and name[0] cuts
// a surrogate pair in half.
const initialsOf = name => {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '';
  const letters = words.length > 1
    ? [...words[0]][0] + [...words[1]][0]
    : [...words[0]].slice(0, 2).join('');
  return letters.toUpperCase();
};

// `size` is '' -- the row and the byline, which are the same size -- or 'xl',
// which is only the profile card. Always aria-hidden: the name it stands for
// is written next to it every time it is used, and a reader that says "PN,
// Priya Nair" has read the row twice.
function face(name, size) {
  // Trimmed once, so the hue and the letters are made of the same string --
  // " Priya Nair " and "Priya Nair" were coming out two different colours --
  // and so a name that is nothing but spaces lands in the branch below rather
  // than drawing an empty coloured disc.
  const who = (name || '').trim();
  const el = document.createElement('span');
  el.className = 'face' + (who ? '' : ' none') + (size ? ' ' + size : '');
  el.setAttribute('aria-hidden', 'true');
  if (!who) return el;
  el.style.setProperty('--h', nameHue(who));
  el.textContent = initialsOf(who);
  return el;
}

// A byline with a face on the front of it. Every list in this app writes who
// said a thing the same way -- "name · 4 min ago" in the muted small -- so the
// face goes on in one place too.
function saidByFace(name, when) {
  const p = document.createElement('p');
  p.className = 'meta';
  p.append(face(name), document.createTextNode(
    (name || 'Anonymous') + ' · ' + ago(when, NOW)));
  return p;
}

// One vote per person per item -- the votes primary key says so, and this is
// only the switch. The count lives inside the control, so pressing it and
// seeing what it did are the same place.
// `answer` switches which thing is being voted for -- an upload, or somebody's
// answer to a doubt. One control and one endpoint, because it is one table:
// what changes is the word the payload is keyed by and what gets refetched
// afterwards. A second vote button would be a second place for "one per
// person" to be drawn differently from how the database counts it.
function voteBtn(u, answer) {
  const b = document.createElement('button');
  b.className = 'vote' + (u.voted ? ' on' : '');
  b.setAttribute('aria-pressed', u.voted ? 'true' : 'false');
  b.setAttribute('aria-label', (u.voted ? 'Remove your upvote from ' : 'Upvote ')
                             + (u.name || 'this answer'));
  b.innerHTML = '▲ <span class="n"></span>';
  b.querySelector('.n').textContent = u.votes;
  b.onclick = async () => {
    b.disabled = true;
    try {
      const r = await fetch('/vote', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(u.post ? {post: u.id, on: !u.voted}
                             : answer ? {answer: u.id, on: !u.voted}
                                      : {id: u.id, on: !u.voted}),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || 'could not register that vote');
      // Refetch rather than patch: the vote changes the ranking too, and one
      // source of order beats two that can disagree.
      // Refetch rather than patch, whichever of the three it was: the vote
      // changes the ranking too, and one source of order beats two.
      if (u.post) { WALLS[wallOn] = null; render(); }
      else await (answer ? loadDoubts() : refresh());
    } catch (e) {
      b.disabled = false;
      busyDone(e.message);
    }
  };
  return b;
}

// A thread on an uploaded file. Doubts (0025) already gave a lecture note its
// comment section; a PDF of last year's paper had nowhere at all to say "page
// 3 is the wrong year", which is the thing people most want to say. So: the
// same thread, the same table, the same two levels -- opened in place under
// the row rather than on a screen of its own, because the file is what you
// are looking at and a comment on it is not somewhere else.
function commentsBtn(id, name) {
  const b = document.createElement('button');
  b.className = 'cmt';
  b.textContent = 'Comments';
  b.setAttribute('aria-label', 'Comments on ' + name);
  b.onclick = () => {
    const row = b.closest('.row');
    const open = row.nextElementSibling
                 && row.nextElementSibling.classList.contains('filethread');
    if (open) {
      row.nextElementSibling.remove();
      b.classList.remove('open');
      b.setAttribute('aria-expanded', 'false');
      threadOn = null;      // nothing is open, so a stray refetch draws nothing
      return;
    }
    const box = document.createElement('div');
    box.className = 'thread filethread';
    row.parentNode.insertBefore(box, row.nextSibling);
    b.classList.add('open');
    b.setAttribute('aria-expanded', 'true');
    loadDoubts({material: id}, box, 'Comments');
  };
  return b;
}

function fileRow(u, s) {
  const el = document.createElement('div');
  el.className = 'row';
  el.style.setProperty('--h', hue(s.code));
  // A photo shows itself. The file already on the shelf is the thumbnail:
  // nothing is resized or stored twice, and loading="lazy" means only the
  // photos actually scrolled to are ever fetched.
  const pic = IS_IMAGE.test(u.name);
  el.innerHTML = '<i class="tick"></i>'
               + (pic ? '<img class="thumb" loading="lazy" alt="">' : '')
               + '<a class="name" target="_blank" rel="noopener"><b></b><small></small></a>';
  if (pic) el.querySelector('img').src = u.path;
  const a = el.querySelector('a');
  a.href = u.path;
  a.querySelector('b').textContent = u.name;
  a.querySelector('small').textContent = u.by ? 'added by ' + u.by : 'file';
  // No id means no row behind it: a static export, or a file the database has
  // not adopted. Showing a vote button that cannot work is worse than none,
  // and the same is true of a remove button with nothing to remove.
  if (u.id) el.appendChild(voteBtn(u));
  if (u.id) el.appendChild(commentsBtn(u.id, u.name));
  if (u.id && atLeast('admin')) {
    const dot = u.name.lastIndexOf('.');
    el.appendChild(renameBtn(dot > 0 ? u.name.slice(0, dot) : u.name, (v, btn) =>
      renameItem({kind: 'material', id: u.id, name: v}, btn)));
    el.appendChild(removeBtn(async (btn) => {
      try {
        const r = await fetch('/remove', {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({id: u.id})});
        if (!r.ok) throw new Error(
          (await r.json().catch(() => ({}))).error || 'could not remove that');
        await refresh();
      } catch (e) {
        btn.disabled = false; btn.classList.remove('armed'); btn.textContent = 'Remove';
        busyDone(e.message);
      }
    }));
  }
  return el;
}

// Several files sharing one batch id read as the one thing they are, rather
// than as several rows named after whatever the camera or the phone called
// each of them. A file with no batch is unaffected -- it renders through
// fileRow exactly as every upload always has, and that is the common case:
// grouping only ever happens where an upload actually asked for it.
// The sitting you are revising for, in the order you meet them. End term
// leads because it is the one worth the most and the one people hunt for.
const EXAMS = [['end', 'End'], ['mid', 'Mid'], ['mini', 'Mini']];
const SHELF = {notes: 'notes', slides: 'slides', assignment: 'assignment',
               lab: 'lab', syllabus: 'syllabus', book: 'book'};

// The subject screen: the same year-by-exam grid the papers screen draws, so a
// course with forty sittings is five short cards and not forty rows. The
// seniors' notes and slides are not sittings and keep their rows.
function papersSection(s) {
  needPapers(s.code);
  const all = PAPERS[s.code];
  if (!all || !all.length) return;
  const sittings = all.filter(p => p.kind === 'paper');
  if (sittings.length) {
    heading('Past papers');
    nav.appendChild(paperGrid(sittings));
  }
  block('Also from the archive',
        all.filter(p => p.kind !== 'paper').map(p => paperRow(p, s)));
}

// Which slice of the archive is on screen. Module state and not part of the
// URL: a filter is a lens on one screen rather than a place.
let pFilter = {code: '', exam: ''};

function renderPapers() {
  needAllPapers();
  if (ALL_PAPERS === null)
    return void nav.appendChild(saying('Reading the archive…'));
  if (ALL_PAPERS === 'failed')
    return void nav.appendChild(saying('The archive did not answer.',
      'It needs the server. Nothing is lost, try again in a moment.'));
  if (!ALL_PAPERS.length)
    return void nav.appendChild(saying('No papers here yet.',
      'This fills up from the collections the seniors kept.'));

  // Subjects in the order the student's own list has them, then anything the
  // archive holds that their curriculum does not. One subject at a time: all
  // 296 at once was the clutter this screen was rebuilt to get rid of.
  const have = new Set(ALL_PAPERS.map(p => p.subject_code));
  const codes = DATA.map(s => s.code).filter(c => have.has(c))
    .concat([...have].filter(c => !subjectOf(c)).sort());
  if (!have.has(pFilter.code)) pFilter.code = codes[0];

  const subs = document.createElement('div');
  subs.className = 'pp-subs';
  subs.setAttribute('role', 'tablist');
  for (const c of codes) {
    const s = subjectOf(c);
    const b = document.createElement('button');
    b.className = 'pp-sub';
    b.setAttribute('role', 'tab');
    b.setAttribute('aria-selected', String(c === pFilter.code));
    b.textContent = s ? s.name : c;
    b.onclick = () => { pFilter.code = c; render(); };
    subs.appendChild(b);
  }
  nav.appendChild(subs);
  // The chosen chip in view, or on a phone the fifth subject is chosen and
  // off the right edge.
  requestAnimationFrame(() => subs.querySelector('[aria-selected=true]')
    ?.scrollIntoView({block: 'nearest', inline: 'center'}));

  const mine = ALL_PAPERS.filter(p => p.subject_code === pFilter.code);
  if (pFilter.exam && !mine.some(p => p.exam === pFilter.exam)) pFilter.exam = '';
  const s = subjectOf(pFilter.code);
  const head = document.createElement('div');
  head.className = 'pp-head';
  head.innerHTML = '<div><b></b><small></small></div><div class="seg" role="group" aria-label="Exam"></div>';
  head.querySelector('b').textContent = s ? s.name : pFilter.code;
  head.querySelector('small').textContent =
    pFilter.code + ' · ' + mine.length + (mine.length === 1 ? ' paper' : ' papers');
  const seg = head.querySelector('.seg');
  for (const [k, label] of [['', 'All']].concat(EXAMS)) {
    if (k && !mine.some(p => p.exam === k)) continue;
    const b = document.createElement('button');
    b.textContent = label;
    b.setAttribute('aria-pressed', String(pFilter.exam === k));
    b.onclick = () => { pFilter.exam = k; render(); };
    seg.appendChild(b);
  }
  nav.appendChild(head);

  const shown = mine.filter(p => !pFilter.exam || p.exam === pFilter.exam);
  if (!shown.length)
    return void nav.appendChild(saying('No ' + pFilter.exam + ' papers for this course.',
      'The archive does not have every sitting of every course.'));
  nav.appendChild(paperGrid(shown));
}

// Year cards, newest first; inside each, one line per exam; on each line, one
// tap target per paper, named by what tells it apart from its neighbours.
function paperGrid(papers) {
  const years = new Map();
  for (const p of papers) {
    const y = p.year || 0;
    if (!years.has(y)) years.set(y, []);
    years.get(y).push(p);
  }
  const grid = document.createElement('div');
  grid.className = 'pp-grid';
  for (const y of [...years.keys()].sort((a, b) => (b || -1) - (a || -1))) {
    const card = document.createElement('section');
    card.className = 'pp-year';
    const h = document.createElement('h3');
    h.textContent = y ? y + '–' + String(y + 1).slice(2) : 'Undated';
    card.appendChild(h);
    const inYear = years.get(y);
    const lines = EXAMS.concat([[null, 'Other']]);
    for (const [k, label] of lines) {
      const these = inYear.filter(p => k ? p.exam === k
                                         : !EXAMS.some(([e]) => e === p.exam));
      if (!these.length) continue;
      const row = document.createElement('div');
      row.className = 'pp-row';
      const name = document.createElement('span');
      name.className = 'pp-exam';
      name.textContent = label;
      const chips = document.createElement('div');
      chips.className = 'pp-chips';
      const named = these.map(p => [paperLabel(p), p])
        .sort((a, b) => (a[1].section ? 0 : 1) - (b[1].section ? 0 : 1)
                        || a[0].localeCompare(b[0], undefined, {numeric: true}));
      const seen = {};
      for (const [text, p] of named) {
        seen[text] = (seen[text] || 0) + 1;
        chips.appendChild(paperChip(p, seen[text] > 1 ? text + ' \u00b7 ' + seen[text] : text,
                                    label));
      }
      row.append(name, chips);
      card.appendChild(row);
    }
    grid.appendChild(card);
  }
  return grid;
}

// What tells a paper apart from the others in its cell. The section letter is
// a column, so it wins; the rest is read off the portal's title, which is the
// only place semester, set and supplementary were ever written down.
function paperLabel(p) {
  if (p.section) return p.section;
  const t = p.title || '';
  const bits = [];
  if (/supp/i.test(t)) bits.push('Supp.');
  const sem = t.match(/sem(?:ester)?\s*-?\s*([12])/i);
  if (sem) bits.push('Sem ' + sem[1]);
  if (/online/i.test(t)) bits.push('Online');
  if (/offline/i.test(t)) bits.push('Offline');
  const set = t.match(/set\s*-?\s*(\d)/i);
  if (set) bits.push('Set ' + set[1]);
  if (/workshop/i.test(t)) bits.push('Workshop');
  return bits.join(' ') || 'Paper';
}

function paperChip(p, text, exam) {
  const a = document.createElement('a');
  a.className = 'pp-chip' + (text.length > 2 ? ' wide' : '');
  a.href = p.path;
  a.target = '_blank';
  a.rel = 'noopener';
  a.title = p.title;
  a.textContent = text;
  const ans = answersIn(p);
  if (ans) {
    const dot = document.createElement('i');
    dot.className = 'pp-ans';
    a.appendChild(dot);
  }
  a.setAttribute('aria-label', [{End: 'End term', Mid: 'Mid term', Mini: 'Mini test'}[exam] || exam, p.year ? p.year + '-' + String(p.year + 1).slice(2) : '',
    p.section ? 'Section ' + p.section : text, ans || ''].filter(Boolean).join(' '));
  return a;
}

function paperRow(p, s) {
  const el = document.createElement('div');
  el.className = 'row';
  el.style.setProperty('--h', hue(s.code));
  el.innerHTML = '<i class="tick"></i>'
               + '<a class="name" target="_blank" rel="noopener"><b></b><small></small></a>';
  const a = el.querySelector('a');
  a.href = p.path;
  a.querySelector('b').textContent = p.title;
  a.querySelector('small').textContent = [p.year ? p.year + '-' + String(p.year + 1).slice(2) : '',
    SHELF[p.kind] || p.kind].filter(Boolean).join(' · ');
  return el;
}

// What a title admits about its answers. Title-only, because that is the whole
// of what the portal ever recorded.
function answersIn(p) {
  const t = p.title || '';
  if (/\bms\b|marking\s*scheme/i.test(t)) return 'marking scheme';
  if (/answer|solution|soln/i.test(t)) return 'with answers';
  return null;
}

function groupedFileRows(s) {
  const seen = new Set();
  const rows = [];
  for (const u of s.uploads) {
    if (!u.batch) { rows.push(fileRow(u, s)); continue; }
    if (seen.has(u.batch)) continue;
    seen.add(u.batch);
    rows.push(fileGroupRow(s.uploads.filter(x => x.batch === u.batch), s));
  }
  return rows;
}

// The vote and the remove control both anchor on the first file rather than
// needing a row of their own: a vote says the whole set was useful, which is
// exactly what "one title" already claimed about them, and there is no
// material_groups table for either one to point at instead. Removing loops
// every file in the batch -- the two-tap confirm still guards the whole
// group behind one press, not one per file.
function fileGroupRow(files, s) {
  const el = document.createElement('div');
  el.className = 'row';
  el.style.setProperty('--h', hue(s.code));
  // One thumbnail for the group -- its first photo -- not one per page.
  const cover = files.find(u => IS_IMAGE.test(u.name));
  el.innerHTML = '<i class="tick"></i>'
    + (cover ? '<img class="thumb" loading="lazy" alt="">' : '')
    + '<span class="name"><b></b><small></small></span>';
  if (cover) el.querySelector('img').src = cover.path;
  const anchor = files[0];
  el.querySelector('b').textContent = anchor.title || (files.length + ' files');
  const small = el.querySelector('small');
  small.textContent = '';
  if (anchor.by) small.append(anchor.by + ' · ');
  files.forEach((u, i) => {
    if (i) small.append(', ');
    const a = document.createElement('a');
    a.href = u.path; a.target = '_blank'; a.rel = 'noopener';
    a.className = 'batch-link';
    a.textContent = u.name;
    small.appendChild(a);
  });
  if (anchor.id) el.appendChild(voteBtn(anchor));
  // The thread anchors on the first file, the same way the vote does: one
  // batch was one thing somebody added, and a comment is about that thing.
  if (anchor.id) el.appendChild(commentsBtn(anchor.id, anchor.title || anchor.name));
  if (anchor.id && atLeast('admin')) {
    el.appendChild(renameBtn(anchor.title || '', (v, btn) =>
      renameItem({kind: 'batch', batch: anchor.batch, name: v}, btn)));
    el.appendChild(removeBtn(async (btn) => {
      try {
        for (const u of files) {
          if (!u.id) continue;
          const r = await fetch('/remove', {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({id: u.id})});
          if (!r.ok) throw new Error(
            (await r.json().catch(() => ({}))).error || 'could not remove that');
        }
        await refresh();
      } catch (e) {
        btn.disabled = false; btn.classList.remove('armed'); btn.textContent = 'Remove';
        busyDone(e.message);
      }
    }));
  }
  return el;
}

function heading(label) {
  const h = document.createElement('h2');
  h.className = 'sect';
  h.textContent = label;
  nav.appendChild(h);
  return h;
}

function block(label, items) {
  if (!items.length) return;
  heading(label);
  const rows = document.createElement('div');
  rows.className = 'rows';
  items.forEach(el => rows.appendChild(el));
  nav.appendChild(rows);
}

// Ink marks a control only an admin may press. One helper, so the treatment
// cannot drift between the places it appears -- and the same two tokens dress
// the admin screen itself, which is the other file this has to agree with.
function inked(row) {
  row.className = 'row adm';
  const tag = document.createElement('span');
  tag.className = 'tag';
  tag.textContent = 'Admin';
  row.appendChild(tag);
  return row;
}

// An honest empty screen: what will be here, and that it is not here yet.
// The first line is the answer and is set as one; everything after it is why.
function saying(...lines) {
  const el = document.createElement('div');
  el.className = 'blank';
  for (const t of lines) {
    const p = document.createElement('p');
    p.textContent = t;
    el.appendChild(p);
  }
  nav.appendChild(el);
}

// One row, two lines of text, optionally a link or a button. Home and the
// contributions screen both wanted this and each had grown a copy.
function line(main, sub, el, h) {
  el = el || document.createElement('div');
  // No subject behind it, no subject colour on it. inked() overwrites this
  // wholesale, which is right: an admin row is marked as one instead.
  el.className = h ? 'row' : 'row plain';
  el.style.setProperty('--h', h || 210);
  el.innerHTML = '<i class="tick"></i><span class="name"><b></b><small></small></span>';
  el.querySelector('b').textContent = main;
  el.querySelector('small').textContent = sub;
  return el;
}

function chip(code) {
  const el = document.createElement('span');
  el.className = 'code';
  el.style.setProperty('--h', hue(code));
  el.textContent = code;
  return el;
}

// A muted line of prose where a row would go: "no classes today" is an answer,
// not an empty list.
function quiet(text) {
  const p = document.createElement('p');
  p.className = 'quiet';
  p.textContent = text;
  return p;
}

// ---- HOME: what a student needs in the ten seconds before a class. -------
// Every section draws from what the page already holds -- DATA, TT, JOBS --
// so the tab paints the instant it is tapped and nothing here waits on a
// request. And nothing here invents: an empty timetable says it is empty
// rather than showing a plausible-looking Monday.

// 1 = Monday .. 6 = Saturday, which is what getDay() already calls them and
// what the timetable table stores, so there is no translation anywhere.
const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const PERIODS = 8;
const dayOf = d => d.getDay();
const slotsOn = (tt, day) =>
  (tt || []).filter(s => s.day === day).sort((a, b) => a.period - b.period);
// What a given DATE holds, which is not the same question as what its weekday
// holds. Ganesh Chaturthi is a Monday and the timetable is full of Mondays, so
// asking by weekday alone draws six classes onto a day the institute closed
// back in July. Every screen that knows its date asks this one instead.
const slotsFor = date => closedOn(date) ? [] : slotsOn(TT, dayOfISO(date));
// Why a day is empty, in the words a student would use. The institute's own
// reason beats "No classes on Monday" -- which is true, but reads like a bug
// on a day the timetable plainly has classes for.
const emptyDay = (date, day) => {
  const shut = closedOn(date);
  const el = document.createElement('div');
  el.className = 'blank';
  const what = document.createElement('p'), why = document.createElement('p');
  what.textContent = shut ? shut.title + ', no classes.'
                          : 'No classes on ' + DAYS[day] + '.';
  why.textContent = shut
    ? 'The institute is closed. Step to another day with the arrows above.'
    : 'Nothing is timetabled. The week is set once for the whole section, so '
      + 'if that is wrong, say so on the wall.';
  el.append(what, why);
  return el;
};

// "Since you last looked" needs a last look. localStorage throws outright in
// private mode, so both touches are guarded: a failure costs the section, not
// the page.
const SEEN_KEY = 'recarve.seen';
let SEEN = null, seenWritten = false;
try { SEEN = +localStorage.getItem(SEEN_KEY) || null; } catch (e) {}

// Stamped from the server's clock, because the mtimes it is compared against
// are that same clock. A phone a few minutes out would otherwise replay
// yesterday's notes as new, or hide this morning's.
function markSeen(now) {
  if (seenWritten || !now) return;
  seenWritten = true;
  try { localStorage.setItem(SEEN_KEY, String(now)); } catch (e) {}
}

// WHAT'S COMING, on the tab that owns the calendar. The institute's own dates,
// reduced to the one thing a student actually wants from them: how far away is
// the next thing that matters. Nothing to show until ATT has answered -- there
// is no guessed countdown, the same way there is no guessed timetable.
//
// A society's fest is NOT in this list, although the server sends it in the
// same array. It has its own list, on Campus, under "Coming up on campus" --
// and printing it in both places was two calendars pretending to be one.
function comingUpBlock() {
  if (!ATT) return;
  const items = (ATT.upcoming || (ATT.next ? [ATT.next] : []))
    .filter(n => n.what !== 'campus');
  if (!items.length) return;
  block('Coming up', items.map(n => {
    const row = line(n.title, [whenSays(n), n.where].filter(Boolean).join(' · '),
                     document.createElement('div'));
    // A tear-off date, so the list reads as a calendar and not as prose.
    const tile = document.createElement('span');
    tile.className = 'tile';
    tile.innerHTML = '<small></small><b></b>';
    tile.querySelector('small').textContent = MONTHS[+n.date.slice(5, 7) - 1].slice(0, 3);
    tile.querySelector('b').textContent = +n.date.slice(8);
    row.insertBefore(tile, row.firstChild);
    return row;
  }));
}

// How far away a calendar entry is, in the words a student would use -- and
// honest about a window already running rather than naming the day it ends.
function whenSays(n) {
  const today = attToday();
  const days = Math.round(
    (new Date(n.date + 'T00:00:00') - new Date(today + 'T00:00:00')) / 86400000);
  const ends = Math.round(
    (new Date(n.ends + 'T00:00:00') - new Date(today + 'T00:00:00')) / 86400000);
  const short = d => MONTHS[+d.slice(5, 7) - 1].slice(0, 3) + ' ' + (+d.slice(8));
  if (days <= 0 && ends >= 0) return ends > 0 ? 'On now · ends ' + short(n.ends) : 'On now · ends today';
  if (days === 1) return 'Tomorrow';
  return 'In ' + days + ' days · ' + short(n.date);
}

// This week, Monday to Sunday, as seven days you can tap straight into. A dot
// is a day with classes on your own timetable; red is a day the institute
// closed. Nothing here is fetched -- it is the timetable and calendar Home
// already holds, drawn as a week instead of as a list.
function calendarWeek() {
  const today = attToday();
  const back = (dayOfISO(today) + 6) % 7;          // days since Monday
  const monday = shiftDay(today, -back);
  const box = document.createElement('div');
  box.className = 'cal';
  const head = document.createElement('div');
  head.className = 'mon';
  head.innerHTML = '<b></b><small></small>';
  head.querySelector('b').textContent =
    MONTHS[+today.slice(5, 7) - 1] + ' ' + today.slice(0, 4);
  head.querySelector('small').textContent = 'This week';
  box.appendChild(head);
  const week = document.createElement('div');
  week.className = 'week';
  let why = null;
  for (let i = 0; i < 7; i++) {
    const date = shiftDay(monday, i);
    const shut = closedOn(date);
    const n = TT ? slotsFor(date).length : 0;
    const b = document.createElement('button');
    b.className = [date === today ? 'today' : '', date < today ? 'past' : '',
                   shut ? 'off' : n ? 'has' : ''].filter(Boolean).join(' ');
    b.innerHTML = '<small></small><b></b><i></i>';
    b.querySelector('small').textContent = DAYS[dayOfISO(date)].slice(0, 1);
    b.querySelector('b').textContent = +date.slice(8);
    b.setAttribute('aria-label', dayName(date) + (shut ? ', ' + shut.title
      : n ? ', ' + n + (n === 1 ? ' class' : ' classes') : ', no classes'));
    b.onclick = () => { dayDate = date; go('classes', 'day'); };
    if (shut && !why) why = DAYS[dayOfISO(date)] + ': ' + shut.title + ', no classes';
    week.appendChild(b);
  }
  box.appendChild(week);
  if (why) {
    const p = document.createElement('p');
    p.className = 'why';
    p.textContent = why;
    box.appendChild(p);
  }
  heading('Calendar');
  nav.appendChild(box);
}

// Section I's bell. Seven periods and a lunch break, off the institute's
// timetable notice (w.e.f. 24/8/2026). Period 8 exists in the section grid
// but has no time here, so it never reaches the timeline.
// ponytail: one section's bell hardcoded; a table when a second section joins.
const PERIOD_TIMES = {1: ['09:00', '09:55'], 2: ['10:00', '10:55'], 3: ['11:00', '11:55'],
  4: ['12:00', '12:55'], 5: ['14:30', '15:25'], 6: ['15:30', '16:25'], 7: ['16:30', '17:25']};
const LUNCH = ['13:00', '14:30'];
const mins = t => +t.slice(0, 2) * 60 + +t.slice(3);
const hhmm = m => { const h = Math.floor(m / 60), mm = String(m % 60).padStart(2, '0');
  return ((h + 11) % 12 + 1) + ':' + mm; };
const long = m => (m >= 60 ? Math.floor(m / 60) + ' h' + (m % 60 ? ' ' + m % 60 + ' min' : '')
                           : m + ' min');

// The day's classes as blocks: back-to-back periods of the same subject (a
// three-hour lab) become one block, never across lunch -- the break is real.
function timelineBlocks(date) {
  const blocks = [];
  for (const sl of slotsFor(date)) {
    const t = PERIOD_TIMES[sl.period];
    if (!t || !subjectOf(sl.code)) continue;
    const [a, b] = [mins(t[0]), mins(t[1])];
    const last = blocks[blocks.length - 1];
    if (last && last.code === sl.code && a - last.end <= 5) { last.end = b; last.periods.push(sl.period); }
    else blocks.push({code: sl.code, start: a, end: b, periods: [sl.period]});
  }
  return blocks;
}

function dayTimeline(date) {
  const blocks = timelineBlocks(date);
  if (!blocks.length) return null;
  const top = mins('09:00'), bottom = mins('17:30');
  const box = document.createElement('div');
  box.className = 'tl';
  box.style.height = 'calc(' + (bottom - top) + ' * var(--px))';
  const at = (el, a, b) => {
    el.style.top = 'calc(' + (a - top) + ' * var(--px))';
    if (b !== undefined) el.style.height = 'calc(' + (b - a) + ' * var(--px) - 3px)';
    box.appendChild(el);
  };
  for (let h = 9; h <= 17; h++) {
    const hr = document.createElement('div');
    hr.className = 'hr';
    hr.innerHTML = '<span></span>';
    hr.querySelector('span').textContent = ((h + 11) % 12 + 1) + (h < 12 ? ' am' : ' pm');
    at(hr, h * 60);
  }
  const lunch = document.createElement('div');
  lunch.className = 'lunch';
  lunch.textContent = 'Lunch';
  at(lunch, mins(LUNCH[0]), mins(LUNCH[1]));
  // What the timeline is for: seeing the holes. A gap of half an hour or more
  // between two classes that lunch does not already explain says so.
  const edges = blocks.map(b => [b.start, b.end]).concat([[mins(LUNCH[0]), mins(LUNCH[1])]])
    .sort((x, y) => x[0] - y[0]);
  for (let i = 1; i < edges.length; i++) {
    const gap = edges[i][0] - edges[i - 1][1];
    if (gap >= 30 && edges[i - 1][1] >= blocks[0].start && edges[i][0] <= blocks[blocks.length - 1].end) {
      const f = document.createElement('div');
      f.className = 'free';
      f.textContent = 'Free · ' + long(gap);
      at(f, edges[i - 1][1], edges[i][0]);
    }
  }
  const nowD = new Date(), now = nowD.getHours() * 60 + nowD.getMinutes();
  const isToday = date === attToday();
  for (const b of blocks) {
    const s = subjectOf(b.code);
    const ev = document.createElement('button');
    ev.className = 'ev' + (isToday && now >= b.end ? ' done' : '');
    ev.style.setProperty('--h', hue(b.code));
    ev.innerHTML = '<b></b><small></small>';
    ev.querySelector('b').textContent = s.name;
    ev.querySelector('small').textContent =
      hhmm(b.start) + '–' + hhmm(b.end) + ' · ' + long(b.end - b.start) + ' · ' + b.code;
    ev.setAttribute('aria-label', s.name + ', ' + hhmm(b.start) + ' to ' + hhmm(b.end));
    ev.onclick = () => go('classes', b.code);
    at(ev, b.start, b.end);
  }
  if (isToday && now >= top && now <= bottom) {
    const n = document.createElement('div');
    n.className = 'now';
    n.setAttribute('aria-label', 'Now');
    at(n, now);
  }
  return box;
}

// A section heading, the day drawn as a timeline, then the rows that act on
// it (marking attendance) underneath -- the timeline is for seeing the day,
// the rows are still where a mark is made.
function blockWithTimeline(label, date, rows) {
  heading(label);
  const tl = TT && TT.length ? dayTimeline(date) : null;
  if (tl) nav.appendChild(tl);
  if (!rows.length) return;
  const box = document.createElement('div');
  box.className = 'rows';
  rows.forEach(el => box.appendChild(el));
  nav.appendChild(box);
}

// 1. TODAY. Empty is the honest first state: nobody has put the section's
// week in yet, and the institute PDF's columns are ambiguous enough that a
// guessed one would quietly file lectures under the wrong subject.
function todayBlock() {
  // The server's day when there is a server, because the marks written from
  // this block are stamped with the server's date. A handset a few hours out
  // would otherwise list Sunday's classes and file them under Monday.
  const day = ATT ? dayOfISO(ATT.today) : dayOf(new Date());
  if (TT === null) return block('Today', [waitline('Checking your timetable…')]);

  if (!TT.length) {
    if (!live) {
      return block('Today', [quiet('Your timetable needs the server. Run: notes.py serve')]);
    }
    return block('Today', [quiet('Your section\u2019s week has not been set up yet. A class rep or an admin sets it once, for everybody.')]);
  }

  // Today's classes, markable where they already are. This is the screen a
  // student opens between periods, so attendance is a tap on the row that is
  // in front of them rather than a trip to a tab. What the row used to say --
  // how many lectures the subject holds -- is the same string the Classes tab
  // prints under every subject, one tap away; what it says instead is the one
  // thing only this row can act on.
  const date = attToday();
  const slots = slotsFor(date);
  const rows = [];
  for (const slot of slots) {
    if (!subjectOf(slot.code)) continue;       // a code the library dropped
    // No server, no marks: Home still has to draw today rather than offer a
    // control that cannot save. Calling a class off is not offered here at all.
    if (!ATT) {
      const s = subjectOf(slot.code);
      const has = s.notes.length || s.uploads.length;
      const b = line(s.name, 'Period ' + slot.period + ' · '
                     + (has ? counts(s) : 'no notes yet'),
                     document.createElement('button'), hue(slot.code));
      b.appendChild(chip(slot.code));
      b.onclick = () => go('classes', slot.code);
      rows.push(b);
      continue;
    }
    rows.push(classRow(date, slot, false));
  }
  if (ATT) {
    const all = allPresentRow(date, slots);
    if (all) rows.push(all);
  }
  if (!rows.length) rows.push(emptyDay(date, day));
  blockWithTimeline('Today \u00b7 ' + DAYS[day], date, rows);
}

// 2. NEEDS YOU. Only what is actually waiting on a person: a transcription
// still running or failed, and -- for an admin -- classmates at the door.
// Nothing waiting means no section at all, which block() already does.
function needsBlock() {
  const rows = JOBS.filter(j => j.state !== 'done').map(jobRow);
  if (PENDING) {
    const a = document.createElement('a');
    a.href = '/admin';
    rows.push(inked(line(PENDING === 1 ? 'One person is waiting to be let in'
                                       : PENDING + ' people are waiting to be let in',
                         'Tap to approve them', a)));
  }
  block('Needs you', rows);
}

// 3. NEW SINCE YOU LAST LOOKED. No last look, no section -- on a first visit
// everything is new, and saying so is noise rather than news.
function newBlock() {
  if (!SEEN) return;
  const fresh = [];
  for (const s of DATA) {
    for (const n of s.notes) {
      if (!(n.at > SEEN)) continue;
      fresh.push([n.at, () => {
        const b = line(n.title, n.by ? 'recorded by ' + n.by
                       : (n.kind === 'revision' ? 'revision sheet' : 'lecture'),
                       document.createElement('button'), hue(s.code));
        b.appendChild(chip(s.code));
        b.onclick = () => go('classes', s.code, n.title);
        return b;
      }]);
    }
    for (const u of s.uploads) {
      if (!(u.at > SEEN)) continue;
      fresh.push([u.at, () => {
        const a = document.createElement('a');
        a.href = u.path; a.target = '_blank'; a.rel = 'noopener';
        const el = line(u.name, u.by ? 'added by ' + u.by : 'notes or slides',
                        a, hue(s.code));
        el.appendChild(chip(s.code));
        return el;
      }]);
    }
  }
  if (!fresh.length) {
    return block('New since you last looked',
                 [quiet('Nothing new since you last looked.')]);
  }
  fresh.sort((a, b) => b[0] - a[0]);
  const rows = fresh.slice(0, 12).map(f => f[1]());
  if (fresh.length > rows.length) {
    rows.push(quiet('and ' + (fresh.length - rows.length) + ' more, under Classes.'));
  }
  block('New since you last looked', rows);
}

// 2b. THE WAY IN, for the one person who runs the class. An admin opens Home
// like everybody else, and the panel used to be reachable only from the Me tab
// or by typing the URL. Inked, like every other admin-only control, and shown
// on ROLE alone -- which is the server's word, arriving on /data. Hiding it is
// a courtesy either way: /admin is in ROLE_REQUIRED, so curl gets the same 403
// a student's browser would.
function adminBlock() {
  if (!atLeast('admin')) return;
  const a = document.createElement('a');
  a.href = '/admin';
  block('Admin', [inked(line('Class admin',
    'Members, roles, the invite link, and anything reported', a))]);
}

// Home is today, and only today: what you have on, what is waiting on you,
// what the section has been told, and what has landed since you last looked.
// Everything here that has a life of its own elsewhere is a way INTO that
// place rather than a second copy of it -- the timetable, the catch-up screen
// and every note on this screen open on the tab that owns them.
function renderHome() {
  // Writing a notice takes the screen over, the same way Campus's three forms
  // take theirs: it is one thing at a time on a phone. A student who types the
  // URL falls through to the board, which is the same answer /announce gives
  // them -- hiding the composer is a courtesy, the gate is the lock.
  if (view.compose && atLeast('cr')) return renderCompose();
  todayBlock();
  needsBlock();
  renderAnnouncements();
  adminBlock();
  // 4. An empty library is a real state on day one, and a blank screen reads
  // as a broken app. Say what the first thing to do is.
  if (!DATA.some(s => s.notes.length || s.uploads.length)) {
    return saying('Nothing in the library yet.',
                  'Tap + to record a class, or to add slides you already have. '
                  + 'It files itself under the subject you pick, and the notes '
                  + 'come back here when the Mac has finished making them.');
  }
  newBlock();
}

// Notes a student asked to find again. Matched against DATA rather than
// trusted on its own: a bookmark can outlive the note it points to (a
// professor's file renamed, a revision sheet since replaced), and a row for a
// note that is no longer there is silently skipped rather than shown as a
// dead link with nothing behind it.
//
// Its own screen, under the avatar, rather than a block near the bottom of
// Home: what you saved is yours, and Home is today.
function savedScreen() {
  const rows = [];
  for (const b of BOOKMARKS) {
    const s = subjectOf(b.code);
    const n = s && s.notes.find(x => x.title === b.title);
    if (!n) continue;
    const row = line(n.title, s.code, document.createElement('button'), hue(s.code));
    row.appendChild(chip(s.code));
    row.onclick = () => go('classes', s.code, n.title);
    rows.push(row);
  }
  if (!rows.length) {
    return saying('Nothing saved yet.',
      'Tap Save while you are reading a lecture and it lands here, so the one '
      + 'you want the night before an exam is two taps from anywhere.');
  }
  const box = document.createElement('div');
  box.className = 'rows';
  rows.forEach(el => box.appendChild(el));
  nav.appendChild(box);
}

// ---- About: who made this, and what it is standing on. ------------------
//
// Under Me and not a tab: it is read once, by somebody who wondered. It asks
// the server for nothing -- every line is a constant -- so it is the one
// screen that is whole on a train with no signal, which is also the only
// honest place to put credits. What the app borrows, it owes whether or not
// there is a network to fetch an acknowledgement over.
const MADE_BY = {
  name: 'Anirudh Sahu',
  says: 'First-year ECE at MANIT Bhopal. Built recarve because the notes for '
      + 'a lecture you missed were in eleven WhatsApp chats and none of them '
      + 'were searchable.',
  links: [
    ['LinkedIn', 'anirudh-sahu', 'https://www.linkedin.com/in/anirudh-sahu-4b245327b/'],
    ['Instagram', '@anirudh_sahu_12', 'https://www.instagram.com/anirudh_sahu_12/'],
  ],
};

// Named, not listed: each line says what the thing actually does here, because
// a credit that does not say what was borrowed is a logo wall.
const CREDITS = [
  ['MANIT Bhopal',
   'The institute timetable, the scheme and the academic calendar every screen '
   + 'is built on, Dr. Fozia Z. Haque, Prof. I/c Institute Time-Table'],
  ['Claude, by Anthropic',
   'Writes the notes and the practice questions from a recording. Once per '
   + 'lecture, on the Mac, and then cached, never per student'],
  ['Whisper, by OpenAI',
   'Turns the recording into text before Claude ever sees it, through '
   + 'mlx-whisper on Apple silicon'],
  ['PostgreSQL',
   'Every row here, and the row level security that is the actual wall between '
   + 'one section and another'],
  ['Tailwind CSS', 'The styling language this whole interface is written in'],
  ['marked', 'Renders the notes out of Markdown'],
  ['KaTeX', 'Renders the mathematics'],
  ['Google Maps', 'Draws the campus on the map screen'],
  ['psycopg', 'The Postgres driver underneath all of it'],
  ['Everyone in the section',
   'Every upload, every answered doubt and every recording. The library is '
   + 'not the app\u2019s, it is theirs, and the app is where they put it'],
];

function aboutScreen() {
  const who = [line(MADE_BY.name, MADE_BY.says)];
  for (const [what, handle, href] of MADE_BY.links) {
    const a = document.createElement('a');
    a.href = href;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    who.push(line(what, handle, a));
  }
  block('Made by', who);
  block('Standing on', CREDITS.map(([what, does]) => line(what, does)));
  saying('recarve is one file and one database.',
         'If something here is wrong, a period in the wrong hour, a '
         + 'subject under the wrong name, say so on the wall. It is '
         + 'faster to fix than to live with.');
}

// ---- ATTENDANCE ----------------------------------------------------------
// 75% per subject is what MANIT actually checks before it lets you sit the
// paper, so every number here is per subject and there is no overall figure at
// all -- an average nobody is refused an exam over would only be comforting.
//
// THREE STATES, and the third one is the whole point: a period nobody has
// marked is not present and not absent. It counts in neither half of the
// fraction and the page never guesses one, because a silent "present" would
// quietly hand a student a percentage that is wrong in the direction that
// costs them the exam.
//
// WHERE IT LIVES, and why.
//   Marking is on Home, on the classes Home already lists. That is the screen
//   opened between periods with one hand, and a mark that costs a trip to
//   another tab is a mark nobody makes.
//   The NUMBER is with the subject, under Classes -- that is where you go when
//   what you want to know is where you stand in Chemistry.
//   Catching up on a week you forgot is its own level under Home, beside the
//   timetable editor and reached the same way. It is a rarer job, it needs a
//   date picker, and a date picker has no business on the ten-second screen.
// Nothing here appears anywhere else: not on the board, not in the admin
// panel, not in anybody else's /data. It is a private note to yourself.
let BOOKMARKS = [];            // {code, title} pairs this student has saved
const isSaved = (code, title) => BOOKMARKS.some(b => b.code === code && b.title === title);

// The Save button lives in the dock, which sits outside #nav -- render() never
// touches it, the same reason practice.hidden is set directly in openNote()
// rather than through a redraw.
function paintSaveBtn() {
  const btn = document.getElementById('save');
  if (!btn || !current) return;
  const on = isSaved(currentCode, current.title);
  btn.textContent = on ? 'Saved' : 'Save';
  btn.setAttribute('aria-pressed', on ? 'true' : 'false');
}

async function toggleBookmark(code, title, on, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/bookmark', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({subject: code, title, on})});
    if (r.ok) BOOKMARKS = (await r.json()).bookmarks || [];
  } catch (e) {}
  if (btn) btn.disabled = false;
  paintSaveBtn();
  // #list sits alongside #read, not inside it, so a redraw here reaches the
  // desktop two-pane list -- where a "Saved" row is exactly the kind of thing
  // that ought to update the moment it changes -- and costs the mobile reading
  // view nothing: #list is hidden there while reading.
  render();
}

let ATT = null;                // the server's attendance payload; null until it answers
let PAPER_COUNTS = {};         // {code: n} past papers, for the subject list
let attDate = null;            // which day the catch-up screen is showing
let dayDate = null;            // and which day the day view under Classes is on

const attOf = code => (ATT && ATT.subjects.find(a => a.code === code)) || null;
// 'YYYY-MM-DD' in the phone's own timezone. toISOString() is UTC and would
// hand back yesterday for everybody east of Greenwich before half past five in
// the morning -- which is every student this app has.
const isoDay = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
                    + '-' + String(d.getDate()).padStart(2, '0');
// The server's date, not the handset's: it is the date the marks are stamped
// with, and a phone a day out would otherwise offer to mark tomorrow.
const attToday = () => (ATT && ATT.today) || isoDay(new Date());
// Midday, so no daylight-saving shift can move the date across a midnight.
const dayOfISO = s => new Date(s + 'T12:00:00').getDay();
// One day either side, through midday for the same reason.
const shiftDay = (date, n) =>
  isoDay(new Date(new Date(date + 'T12:00:00').getTime() + n * 86400000));
// Whether a mark on this date could be saved at all: no server behind the page,
// a class that has not happened, or a day further back than the payload reaches
// and the server refuses it -- _att_date says the same three things.
const markable = date => !!ATT && date <= ATT.today
                         && date >= shiftDay(ATT.today, -ATT.window)
                         && !closedOn(date);
// The institute's own answer for this date -- a holiday, the mid-sem break, an
// exam window -- or null on an ordinary day. Not a mark and not a cancellation:
// nobody in this section decided it and nobody here can undo it.
const closedOn = date =>
  (ATT && ATT.closed ? ATT.closed.find(c => c.date === date) : null) || null;
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                'August', 'September', 'October', 'November', 'December'];
// What to call a day on a heading. The three days a student thinks of by name
// get their name; anything else is dated, because "Tuesday" three weeks back is
// not an answer to which Tuesday.
const dayName = date =>
    date === attToday() ? 'Today'
  : date === shiftDay(attToday(), -1) ? 'Yesterday'
  : date === shiftDay(attToday(), 1) ? 'Tomorrow'
  : DAYS[dayOfISO(date)] + ' ' + (+date.slice(8)) + ' ' + MONTHS[+date.slice(5, 7) - 1];
const markAt = (date, period) =>
  (ATT ? ATT.marks.find(m => m.date === date && m.period === period) : null);
const offAt = (date, code, period) =>
  (ATT ? ATT.off.find(o => o.date === date && o.period === period && o.code === code)
       : null);
const STATE_WORD = {present: 'Present', absent: 'Absent'};

// Every write here answers with the whole attendance payload, so the page
// never recomputes a percentage of its own. One place does that arithmetic and
// it is the server; two would eventually disagree, and this is the number a
// student plans a term around.
async function attPost(url, payload, btns) {
  btns.forEach(b => { b.disabled = true; });
  try {
    const r = await fetch(url, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || 'could not save that');
    ATT = d;
    render();
  } catch (e) {
    btns.forEach(b => { b.disabled = false; });
    busyDone(e.message);
  }
}

// The control. Two buttons; pressing neither is the third state, and pressing
// the one you are already in clears it -- which is the only way back to
// not-yet-marked, and a mistap has to have one.
function markCtl(date, slot, mayCancel) {
  const gone = offAt(date, slot.code, slot.period);
  if (gone) {
    const el = document.createElement(mayCancel ? 'button' : 'span');
    el.className = 'off';
    el.textContent = 'Class off';
    if (mayCancel) {
      el.setAttribute('aria-label', 'Put period ' + slot.period + ' back');
      el.onclick = () => attPost('/cancelled',
        {date, period: slot.period, code: slot.code, off: false}, [el]);
    }
    return el;
  }
  const now = markAt(date, slot.period);
  const box = document.createElement('div');
  box.className = 'mark';
  const btns = [];
  for (const [state, glyph, label] of [['present', '✓', 'Present'],
                                       ['absent', '✕', 'Absent']]) {
    const b = document.createElement('button');
    b.className = state === 'present' ? 'yes' : 'no';
    b.textContent = glyph;
    b.setAttribute('aria-pressed', now && now.state === state ? 'true' : 'false');
    b.setAttribute('aria-label',
                   label + ', period ' + slot.period + ', ' + slot.code);
    b.onclick = () => attPost('/attendance', {marks: [{date, period: slot.period,
      state: now && now.state === state ? 'clear' : state}]}, btns);
    btns.push(b);
    box.appendChild(b);
  }
  // Calling a class off is trusted, and it is offered only on the catch-up
  // screen: three controls is one too many for the row you tap on the way into
  // a lecture, and this is the rarer act by far.
  if (mayCancel) {
    const off = document.createElement('button');
    off.className = 'no';
    off.textContent = '–';
    off.setAttribute('aria-label',
                     'Period ' + slot.period + ' did not happen, for everybody');
    off.onclick = () => attPost('/cancelled',
      {date, period: slot.period, code: slot.code, off: true}, btns.concat(off));
    box.appendChild(off);
  }
  return box;
}

// One class on one day: what it is, what it is marked as in words, and the
// buttons. A div rather than a button around the lot, because a button inside
// a button is not a thing any browser will give you -- so the name is the
// tappable part and it still opens the subject.
function classRow(date, slot, mayCancel) {
  const s = subjectOf(slot.code);
  const gone = offAt(date, slot.code, slot.period);
  const m = markAt(date, slot.period);
  const el = document.createElement('div');
  el.className = 'row';
  el.style.setProperty('--h', hue(slot.code));
  el.innerHTML = '<i class="tick"></i>'
               + '<button class="name"><b></b><small></small></button>';
  el.querySelector('b').textContent = s ? s.name : slot.code;
  // The state in words as well as in the pressed button. Colour and shape
  // alone leave it unreadable to whoever cannot see one of them, and this row
  // is the only place the state is ever shown.
  // The reason rides with it when there is one: a cancellation moves the
  // denominator of everybody in the section, and whoever it moved has to be
  // able to read why without asking somebody.
  // "Not marked" only where a mark is a thing this page can see: with no
  // server behind it, or on a day older than the payload reaches, the marks
  // exist and are simply not here -- and saying "Not marked" about them would
  // be the page inventing an answer about the one number that costs an exam.
  const said = gone ? 'Class off' + (gone.reason ? ' · ' + gone.reason : '')
             : m ? STATE_WORD[m.state]
             : markable(date) ? 'Not marked' : '';
  el.querySelector('small').textContent =
    'Period ' + slot.period + (said ? ' · ' + said : '');
  if (s) el.querySelector('.name').onclick = () => go('classes', s.code);
  // Same test, one place: a control that would be refused is worse than none.
  if (markable(date)) el.appendChild(markCtl(date, slot, mayCancel));
  return el;
}

// The one control the day view and the catch-up screen share. A native date
// input for a jump across a month, and an arrow either side for the step that
// is actually taken -- yesterday, tomorrow -- because a student walking out of
// a lecture has one thumb and no patience for a wheel.
function stepDay(date, n, set, min, max) {
  const to = shiftDay(date, n);
  const b = document.createElement('button');
  b.className = 'step';
  b.textContent = n < 0 ? '‹' : '›';
  b.setAttribute('aria-label', n < 0 ? 'The day before' : 'The day after');
  b.disabled = !!(min && to < min) || !!(max && to > max);
  b.onclick = () => set(to);
  return b;
}

function dayPicker(date, set, min, max) {
  const box = document.createElement('div');
  box.className = 'dpick';
  const input = document.createElement('input');
  input.type = 'date';
  input.setAttribute('aria-label', 'Day');
  input.value = date;
  if (min) input.min = min;
  if (max) input.max = max;
  input.onchange = () => set(input.value || date);
  box.append(stepDay(date, -1, set, min, max), input,
             stepDay(date, 1, set, min, max));
  nav.appendChild(box);
}

// One tap for a whole day. Somebody catching up on a week they forgot has six
// of these to do, and twelve taps a day is the difference between filling it
// in and giving up on it. Offered only where it saves something: one unmarked
// class is already one tap.
function allPresentRow(date, slots) {
  const todo = slots.filter(sl => !offAt(date, sl.code, sl.period)
                                  && !markAt(date, sl.period));
  if (todo.length < 2) return null;
  const b = line('Mark all ' + todo.length + ' present',
                 'Then change the ones you missed',
                 document.createElement('button'));
  b.onclick = () => attPost('/attendance',
    {marks: todo.map(sl => ({date, period: sl.period, state: 'present'}))}, [b]);
  return b;
}

// attended / held, the true percentage, and what it means -- every one of them
// straight off the server, which is the only place that arithmetic happens.
// The percentage is already floored to a tenth there: 74.96% arrives as 74.9
// and is printed as 74.9, because a number that rounds up across the threshold
// would read as safe to somebody who is not.
function attRow(a, showCode) {
  const el = line(a.held ? a.attended + ' of ' + a.held + ' · '
                           + a.pct.toFixed(1) + '%'
                         : 'Nothing marked yet',
                  a.note, document.createElement('button'), hue(a.code));
  el.onclick = () => go('classes', a.code);
  if (a.held) el.classList.add('att');
  if (a.held && !a.ok) {
    el.classList.add('low');
    const f = document.createElement('span');
    f.className = 'flag';
    f.textContent = 'Below 75%';
    el.appendChild(f);
  }
  if (showCode) el.appendChild(chip(a.code));
  return el;
}

// The level under Home: one day at a time, any day in the window, so a week
// that was forgotten can be filled in after the fact. People forget, and a
// screen that only marked today would be filled in by nobody.
function renderAttendance() {
  if (!ATT) return block('Attendance', [waitline('Checking your attendance…')]);
  const date = attDate || attToday();
  // Tomorrow has not happened yet, and further back than the window is further
  // back than the server will take a mark for.
  dayPicker(date, d => { attDate = d; render(); },
            shiftDay(attToday(), -ATT.window), attToday());

  const day = dayOfISO(date);
  const slots = slotsFor(date);
  const rows = slots.map(sl => classRow(date, sl, mayAdd()));
  const all = allPresentRow(date, slots);
  if (all) rows.push(all);
  if (!rows.length) rows.push(emptyDay(date, day));
  block(dayName(date), rows);
  block('Every subject', ATT.subjects.map(a => attRow(a, true)));
}

// ---- THE NOTICE BOARD. Read on Campus, surfaced on Home. ----------------
// Every notice rides on the /data this page already fetches, so neither tab
// spends a request of its own drawing one.
let ANN = [];                   // the board as the server sent it
let NOW = 0;                    // the server's clock, which `at` is stamped by
const READ_SENT = new Set();    // ids already marked read this visit

// The same three lines as the admin screen's, against the same clock: `at` and
// NOW are both the server's seconds, so a handset that is minutes out cannot
// age a notice that went up a moment ago.
function ago(then, now) {
  const d = Math.max(0, (now || 0) - (then || 0));
  if (d < 90) return 'just now';
  if (d < 3600) return Math.round(d / 60) + ' min ago';
  if (d < 172800) return Math.round(d / 3600) + ' h ago';
  return Math.round(d / 86400) + ' days ago';
}

const liveAnn = () => ANN.filter(a => !a.deleted);

// A notice body is the one thing on this page that a person types and the page
// renders as markup, so markdown has to be all that can come out of it. Every
// '<' is escaped before marked sees it, which leaves no way to write a tag at
// all -- <script>, <img onerror>, anything -- and the links marked does build
// are then held to http, mailto and in-page anchors, so [tap](javascript:...)
// lands as a dead link rather than a live one. Notes go through mdInto
// instead: they are generated on this machine and deliberately carry <details>.
function mdSafe(src) {
  return marked.parse(String(src || '').replace(/</g, '&lt;'))
    .replace(/ (href|src)="(?!https?:|mailto:|#)[^"]*"/gi, '');
}

function annCard(a) {
  const el = document.createElement('div');
  el.className = 'ann' + (a.pinned && !a.deleted ? ' pin' : '') + (a.deleted ? ' gone' : '');
  el.innerHTML = '<h3></h3><p class="meta"></p><div class="md"></div>';
  el.querySelector('h3').textContent = a.title;
  const flags = [a.deleted ? 'Hidden' : (a.pinned ? 'Pinned' : ''),
                 a.unread && !a.deleted ? 'New' : ''].filter(Boolean);
  const meta = el.querySelector('.meta');
  if (flags.length) {
    const f = document.createElement('span');
    f.className = 'flag';
    f.textContent = flags.join(' · ');
    meta.append(f, document.createTextNode(' · '));
  }
  meta.appendChild(document.createTextNode(
    a.by + ' · ' + ago(a.at, NOW) + (a.edited ? ' · edited' : '')));
  el.querySelector('.md').innerHTML = mdSafe(a.body);
  // Their own, because that is what the database allows: "class reps edit
  // their own announcements" refuses anybody else's, and offering a button
  // that would be refused is worse than not offering it.
  if (a.mine && atLeast('cr')) {
    const acts = document.createElement('div');
    acts.className = 'acts';
    const edit = document.createElement('button');
    edit.textContent = 'Edit';
    edit.onclick = () => go('home', a.id);
    const del = document.createElement('button');
    del.textContent = a.deleted ? 'Put it back' : 'Delete';
    del.onclick = () => saveAnn({id: a.id, deleted: !a.deleted}, null, del);
    acts.append(edit, del);
    el.appendChild(acts);
  }
  return el;
}

// One write for all four things an admin does to a notice -- post, edit, hide,
// restore -- because they are one row and one policy. The board comes back in
// the answer, so the screen redraws from what was stored and not from what was
// typed.
async function saveAnn(payload, err, btn) {
  // Armed for the whole request, a second tap posts the notice twice to a
  // hundred and ten people, and there is no delete policy to undo it with.
  if (btn) btn.disabled = true;
  busy('Saving…', true);
  try {
    const r = await fetch('/announce', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || 'could not save that');
    ANN = d.announcements || [];
    busyDone(payload.deleted ? 'Hidden. You can still put it back'
                             : (payload.id ? 'Saved' : 'Posted'));
    // Out of the composer the way you came in, exactly like the timetable's.
    if (view.compose) history.back(); else render();
  } catch (e) {
    if (btn) btn.disabled = false;
    busyDone('');
    if (err) err.textContent = e.message;
    else busyDone('Could not save that: ' + e.message);
  }
}

// "I have seen these", sent once per visit for whatever is actually on screen.
// Server-side rather than in localStorage: the same person opens this on a
// phone in a corridor and a laptop that evening, and a notice they have read
// must not be new again on the second one.
function markRead(list) {
  const ids = list.filter(a => a.unread && !a.deleted && !READ_SENT.has(a.id))
                  .map(a => a.id);
  if (!ids.length || !live) return;
  ids.forEach(i => READ_SENT.add(i));
  fetch('/read', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ids}),
  }).then(r => {
    // The marks on screen stay put -- you are looking at them -- but Home must
    // not still be calling them new the moment you go back to it.
    if (r.ok) ANN.forEach(a => { if (ids.includes(a.id)) a.unread = false; });
    else ids.forEach(i => READ_SENT.delete(i));
  }).catch(() => ids.forEach(i => READ_SENT.delete(i)));
}

function renderCompose() {
  // The URL is the whole of the state: '#campus/new', or '#campus/<id>'
  // for one that already exists. Nothing is held in a variable that a
  // back gesture cannot reach.
  const a = ANN.find(x => x.id === view.compose) || {};
  const box = document.createElement('div');
  box.className = 'compose';
  box.innerHTML = '<label for="atitle">Title</label>'
    + '<input id="atitle" maxlength="120" autocomplete="off" '
    + 'placeholder="Chemistry lab moved to Friday">'
    + '<label for="abody">What everyone needs to know</label>'
    + '<textarea id="abody" maxlength="4000" '
    + 'placeholder="Markdown works: **bold**, lists, and links."></textarea>'
    + '<label class="pinrow"><input type="checkbox" id="apin">'
    + '<span>Keep it at the top</span></label>'
    + '<p class="err" id="aerr"></p>'
    + '<div class="go"><button id="acancel">Cancel</button>'
    + '<button id="asave" class="primary"></button></div>';
  nav.appendChild(box);
  const title = box.querySelector('#atitle'), body = box.querySelector('#abody');
  const pin = box.querySelector('#apin'), err = box.querySelector('#aerr');
  title.value = a.title || '';
  body.value = a.body || '';
  pin.checked = !!a.pinned;
  const save = box.querySelector('#asave');
  save.textContent = a.id ? 'Save changes' : 'Post it';
  box.querySelector('#acancel').onclick = () => history.back();
  save.onclick = () => {
    if (!title.value.trim()) return void (err.textContent = 'A notice needs a title.');
    saveAnn({id: a.id, title: title.value, body: body.value, pinned: pin.checked},
            err, save);
  };
}

// The notice board, in full and in one place. It used to be here AND three
// rows deep at the top of Home; a notice you had read then showed up twice and
// a notice you had not showed up twice differently. Home is where it lives.
function renderAnnouncements() {
  heading('Announcements');
  if (atLeast('cr')) {
    const post = line('Post an announcement', 'Every approved classmate sees it',
                      document.createElement('button'));
    post.onclick = () => go('home', 'new');
    const rows = document.createElement('div');
    rows.className = 'rows';
    rows.appendChild(inked(post));
    nav.appendChild(rows);
  }
  // A hidden notice is still on its author's screen, because they are the one
  // who might want it back. Nobody else is sent it at all.
  const list = ANN.filter(a => !a.deleted || a.mine);
  if (!list.length) {
    return saying('Nothing on the notice board yet.',
      atLeast('cr')
        ? 'Anything the whole section needs to know goes here: a moved '
          + 'lab, a deadline, where the lecture is. Everyone approved sees it, '
          + 'and pinned ones stay at the top.'
        : 'This is where the section is told things: a moved lab, a '
          + 'deadline, where a lecture is. Your CR or your admin puts them up, and '
          + 'they are the first thing on this screen until you have read them.');
  }
  list.forEach(a => nav.appendChild(annCard(a)));
  markRead(list);
}

// ---- COMMUNITY: the people. ---------------------------------------------
// The board lives here rather than on your own screens because it is a list of
// other people. Under the avatar is where your own score and the rules behind
// it are, next to your name and your role; a ranking of a hundred and ten
// classmates is not a fact about you. It used to be printed in both places,
// which made the same number mean two things on two screens.
//
// Points are status and nothing else. Nothing on this screen is a key, and the
// empty state says so out loud, because a leaderboard is exactly the place
// somebody would assume otherwise.
let BOARD = null;               // the last /standings answer, or null
let boardWindow = 'all';        // 'all' or 'week'; a toggle, not a URL

// Few, and earned by doing the thing rather than awarded by hand: the ladder is
// the score, and the score is only ever uploads, recordings and votes. Highest
// first, so levelOf() is a find(). The third field is whether the word is worth
// wearing in public -- one on every row of the board is decoration, one on
// three rows out of twenty is somebody being recognised.
const LEVELS = [[100, 'Mainstay', true], [25, 'Regular', true],
                [1, 'Contributor', false], [0, 'New here', false]];
const levelOf = score => LEVELS.find(l => l[0] <= score);
const nextLevel = score => LEVELS.filter(l => l[0] > score).pop();

// The whole reason there is a score at all, said in the row it belongs to.
// Zeroes are dropped: they add nothing to the score, and "0 uploads · 3
// recordings · 0 votes" wraps to three lines on a 390px phone and buries the
// one number that was worth reading.
const breakdown = r => [ROLE_TITLE[r.role] || 'Student'].concat(
  [[r.uploads, 'upload'], [r.recordings, 'recording'], [r.votes_received, 'vote']]
    .filter(part => part[0]).map(part => plural(part[0], part[1]))).join(' · ');

function boardRow(r) {
  const el = document.createElement('div');
  el.className = 'rank' + (r.you ? ' you' : '');
  el.innerHTML = '<span class="pos"></span>'
    + '<span class="name"><b></b><small></small></span>'
    + '<span class="lvl"></span><span class="pts"></span>';
  // No score, no place: everybody who has not started yet shares the last rank,
  // and printing that number would be an invented position.
  el.querySelector('.pos').textContent = r.score ? '#' + r.rank : '-';
  // Twenty classmates, and the only thing telling one row from the next was
  // which number was on it. The face goes between the rank and the name --
  // where a rank reads as "who", not as "how many".
  el.insertBefore(face(r.name), el.querySelector('.name'));
  el.querySelector('b').textContent = r.you ? r.name + ' (you)' : r.name;
  el.querySelector('small').textContent = r.score ? breakdown(r)
    : 'Add a recording or a set of slides and you are on the board';
  const lvl = el.querySelector('.lvl');
  const [, word, worn] = levelOf(r.score);
  lvl.textContent = worn ? word : '';
  lvl.hidden = !worn;
  el.querySelector('.pts').textContent = r.score;
  return el;
}

function windowPicker() {
  const row = document.createElement('div');
  row.className = 'days';
  for (const [key, label] of [['all', 'All time'], ['week', 'This week']]) {
    const b = document.createElement('button');
    b.textContent = label;
    if (key === boardWindow) b.setAttribute('aria-current', 'true');
    b.onclick = () => { boardWindow = key; render(); };
    row.appendChild(b);
  }
  nav.appendChild(row);
}

function drawBoard(box) {
  const b = BOARD[boardWindow] || {top: [], you: null};
  box.innerHTML = '';
  if (!b.top.length) {
    box.className = 'blank';
    const one = document.createElement('p'), two = document.createElement('p');
    one.textContent = boardWindow === 'week'
      ? 'Nobody has added anything this week yet.'
      : 'Nobody has added anything yet.';
    two.textContent = 'The first recording or set of slides puts somebody here. '
      + 'Points are recognition only: every note in the library is open to '
      + 'everyone whatever this says.';
    box.append(one, two);
    return;
  }
  box.className = '';
  b.top.forEach(r => box.appendChild(boardRow(r)));
  // Your own place, pinned, when it is not already up there. Appended to the
  // end of the top twenty without a break it would read as twenty-first.
  if (b.you && !b.top.some(r => r.id === b.you.id)) {
    box.appendChild(quiet('Your place'));
    box.appendChild(boardRow(b.you));
  }
}

// ---- CLUBS, EVENTS AND THE MAP. -----------------------------------------
// ---- THE WALL: what the section says to itself. -------------------------
//
// Two lists off one table (0035). The feed carries a name; a confession does
// not, and the reason it does not is a privilege in Postgres rather than a
// decision on this page -- `authenticated` has no select on posts.author_id at
// all, so the author is not something this file could print by accident. What
// arrives here for a confession is `by: null`, every time, for everybody,
// including the admin who can take it down.
//
// Every body on both lists goes on the page with textContent. This is the one
// screen where somebody deliberately tries a tag, and a post is a sentence,
// not a document: there is nothing to gain from parsing it.
const WALLS = {feed: null, confession: null};
const wallAsked = {feed: false, confession: false};
// The two composers Community has, as URLs: '#community/say' and
// '#community/confess'. Words rather than the list names, because a URL is
// read by a person and "confess" says what the screen is for.
const WALL_COMPOSE = {say: 'feed', confess: 'confession'};
let wallOn = 'feed';        // which of the two Campus is showing

function needWall(kind) {
  if (WALLS[kind] || wallAsked[kind]) return;
  wallAsked[kind] = true;
  fetch('/posts?kind=' + kind)
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(d => { WALLS[kind] = d.posts || []; wallAsked[kind] = false;
                 redrawCommunity(); })
    .catch(() => { WALLS[kind] = 'failed'; wallAsked[kind] = false;
                   redrawCommunity(); });
}

function postCard(x, kind) {
  const el = document.createElement('div');
  el.className = 'post';
  const what = document.createElement('div');
  what.className = 'what';
  const said = document.createElement('p');
  said.className = 'said';
  said.textContent = x.body;
  // "Anonymous" is the honest word and it is written here, not sent: there is
  // no name in the payload to fall back to, and nothing to print if there is.
  // The face is drawn off the same null, so a confession gets the empty ring
  // and never a pair of letters -- there is nothing here to make letters out
  // of, which is the point of the column not being selectable.
  what.append(said, saidByFace(x.by, x.at));
  if (x.photos && x.photos.length) {
    const shots = document.createElement('div');
    shots.className = 'shots';
    x.photos.forEach(ph => {
      const a = document.createElement('a');
      a.href = ph.path; a.target = '_blank'; a.rel = 'noopener';
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.src = ph.path;
      img.alt = ph.name;
      a.appendChild(img);
      shots.appendChild(a);
    });
    what.appendChild(shots);
  }
  // A confession offers its own author nothing, because the page is never
  // told who that is -- `mine` is false on every one of them. An admin takes
  // one down in one tap, which is the whole of moderation here.
  const drop = (x.mine || atLeast('admin')) ? document.createElement('button') : null;
  if (drop) {
    drop.textContent = x.mine ? 'Delete' : 'Take down';
    if (!x.mine) drop.className = 'adm';
    drop.onclick = () => writePost({kind: kind, id: x.id, delete: true}, drop);
    what.appendChild(acts(drop));
  }
  el.append(voteBtn({id: x.id, votes: x.votes, voted: x.voted,
                     name: 'this post', post: true}), what);
  return el;
}

async function writePost(payload, btn, err) {
  if (btn) btn.disabled = true;
  busy('Saving…', true);
  try {
    const r = await fetch('/posts', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || 'could not save that');
    busyDone(payload.delete ? 'Taken down' : 'Posted');
    WALLS[payload.kind] = d.posts || [];
    // Out of the composer the way you came in, exactly like Campus's and the
    // notice board's: what you just wrote is the first thing on the feed
    // behind it, so landing back on the form would hide the result.
    if (view.compose) history.back(); else render();
  } catch (e) {
    if (btn) btn.disabled = false;
    if (err) { err.textContent = e.message; busyDone(''); }
    else busyDone(e.message);
  }
}

// The composer. A box and one button for a confession; the same plus photos
// for the feed, and the photos go through the upload path that already
// exists -- /upload with a batch id -- rather than a second one written for
// this screen. Which means they are subject to the same role as every other
// upload, so a student posts words and is told why in one sentence instead of
// being refused after typing.
function wallComposer(kind) {
  const box = document.createElement('div');
  box.className = 'askbox';
  const ta = document.createElement('textarea');
  ta.maxLength = 2000;
  ta.placeholder = kind === 'feed'
    ? 'Say something to the section: lost something, found something, or ask.'
    : 'Say it without your name on it. Three a day, and an admin can take one '
      + 'down, but nobody is ever told who wrote it.';
  ta.setAttribute('aria-label', kind === 'feed' ? 'Your post' : 'Your confession');
  const err = document.createElement('p');
  err.className = 'err';
  let picker = null, pick = null, shots = null;
  if (kind === 'feed' && mayAdd()) {
    // The subject wheel, without the OS chevron: the wrapper draws the app's
    // own chevron and the select keeps the native wheel behind it.
    const subj = document.createElement('select');
    subj.setAttribute('aria-label', 'Which subject the photos file under');
    DATA.forEach(x => {
      const o = document.createElement('option');
      o.value = x.code;
      o.textContent = x.code + ' \u00b7 ' + x.name;
      subj.appendChild(o);
    });
    pick = document.createElement('div');
    pick.className = 'pick';
    pick.appendChild(subj);
    // Hidden, and opened by the button beside it. .click() on a display:none
    // file input is what the Add sheet has always done; what changes here is
    // that nothing on the screen says "No file chosen" any more.
    picker = document.createElement('input');
    picker.type = 'file';
    picker.accept = 'image/*';
    picker.multiple = true;
    picker.hidden = true;
    const open = document.createElement('button');
    open.type = 'button';
    open.textContent = 'Add photos';
    const said = document.createElement('span');
    said.textContent = 'None picked yet';
    open.onclick = () => picker.click();
    picker.onchange = () => {
      const n = picker.files.length;
      open.textContent = n ? 'Change photos' : 'Add photos';
      said.textContent = n ? plural(n, 'photo') + ' ready to go' : 'None picked yet';
    };
    shots = document.createElement('div');
    shots.className = 'shotpick';
    shots.append(open, said, picker);
  }
  const go = document.createElement('button');
  go.textContent = kind === 'feed' ? 'Post this' : 'Post it anonymously';
  go.onclick = async () => {
    if (!ta.value.trim()) return void (err.textContent = 'Type something first.');
    let batch = null;
    if (picker && picker.files.length) {
      go.disabled = true;
      err.textContent = '';
      batch = crypto.randomUUID();
      const sent = await uploadBatch(picker.files, ta.value.trim().slice(0, 60));
      go.disabled = false;
      if (sent === false) return void (err.textContent = 'Those photos did not go up.');
    }
    writePost({kind: kind, body: ta.value, batch: batch}, go, err);
  };
  box.append(ta);
  if (pick) {
    // Said rather than hidden: a photo posted here is a file in the library,
    // and pretending otherwise would be a surprise the first time somebody
    // found it on a subject's shelf.
    const note = quiet('Photos also file under the subject you pick, on that '
                       + 'subject\u2019s shelf.');
    box.append(pick, shots, note);
  } else if (kind === 'feed') {
    box.append(quiet('Photos are for trusted members. Words are for everybody.'));
  }
  box.append(err, go);
  return box;
}

function wallSection() {
  heading('The section');
  const tabs = document.createElement('div');
  tabs.className = 'acts';
  [['feed', 'Feed'], ['confession', 'Confessions']].forEach(([k, label]) => {
    const b = document.createElement('button');
    b.className = 'cmt' + (wallOn === k ? ' open' : '');
    b.textContent = label;
    b.setAttribute('aria-pressed', wallOn === k ? 'true' : 'false');
    b.onclick = () => { wallOn = k; render(); };
    tabs.appendChild(b);
  });
  const box = document.createElement('div');
  box.className = 'wall';
  box.appendChild(tabs);
  nav.appendChild(box);
  needWall(wallOn);
  const list = WALLS[wallOn];
  if (!list) return void box.appendChild(waitline('Reading what the section has said…'));
  if (list === 'failed') {
    return void box.appendChild(quiet('The section needs the server. '
                                      + 'Run: notes.py serve'));
  }
  if (!list.length) {
    return void box.appendChild(quiet(wallOn === 'feed'
      ? 'Nothing here yet. Lost something? Found something? Tap + and say so.'
      : 'Nothing yet. Tap + to write one. Nobody is ever told it was you.'));
  }
  list.forEach(x => box.appendChild(postCard(x, wallOn)));
}

// ---- DOUBTS, the ones that are not about one lecture. --------------------
// A question about a lecture belongs under that lecture and stays there: the
// note it is about is the context, and moving it here would be a thread with
// its subject cut off. What had nowhere to go at all is the other kind -- a
// question about the subject itself. The server has always kept that thread
// (a doubt hangs off a subject code with no lecture on it, which is what a
// static export and a never-adopted file already fall back to) and nothing on
// this page ever opened it. This is that screen.
let doubtsOn = null;            // whose thread is open, by subject code

function doubtsSection() {
  heading('Doubts');
  const pick = document.createElement('div');
  pick.className = 'days';
  for (const s of DATA) {
    const b = document.createElement('button');
    b.textContent = s.code;
    b.setAttribute('aria-label', s.name);
    if (s.code === doubtsOn) b.setAttribute('aria-current', 'true');
    b.onclick = () => { doubtsOn = doubtsOn === s.code ? null : s.code; render(); };
    pick.appendChild(b);
  }
  nav.appendChild(pick);
  if (!doubtsOn) {
    return void nav.appendChild(quiet('Pick a subject to read what the section '
      + 'has asked about it. A question about one lecture is asked under that '
      + 'lecture, in the library, and stays there.'));
  }
  const box = document.createElement('section');
  box.className = 'thread inset';
  box.setAttribute('aria-label', 'Doubts');
  nav.appendChild(box);
  // Only fetch when it is a different thread from the one already in hand. The
  // wall answering, or a vote landing, repaints this tab -- and re-asking the
  // server on every repaint would be one request per keystroke elsewhere.
  if (threadOn && threadOn.subject === doubtsOn && !threadOn.title
      && !threadOn.material) {
    threadBox = box;
    drawDoubts();
  } else {
    loadDoubts({subject: doubtsOn}, box, subjectOf(doubtsOn).name);
  }
}

// The rest of Campus. Fetched once, on the tab that shows it: three lists,
// none of them wanted by any other tab. The server folds the events into the
// same upcoming array the academic calendar arrives in; Classes filters them
// back out, because a fest belongs to Campus and is printed here.
let CAMPUS = null;              // the last /campus answer, or {failed:true}
let campusAsked = false;

function needCampus() {
  if (CAMPUS || campusAsked || !live) return;
  campusAsked = true;
  fetch('/campus')
    .then(r => r.ok ? r.json() : Promise.reject(new Error(r.status)))
    .then(d => { CAMPUS = d; campusAsked = false; redrawCampus(); })
    .catch(() => { CAMPUS = {failed: true}; campusAsked = false; redrawCampus(); });
}

let PAPERS = {}, papersAsked = {};

// Per subject rather than one payload for all of them: a student opens one
// subject at a time, and the whole shelf is several hundred rows nobody on a
// phone asked for.
function needPapers(code) {
  if (PAPERS[code] || papersAsked[code] || !live) return;
  papersAsked[code] = true;
  fetch('/papers?code=' + encodeURIComponent(code))
    .then(r => r.ok ? r.json() : Promise.reject(new Error(r.status)))
    .then(d => { PAPERS[code] = d.papers; papersAsked[code] = false; redrawSubject(code); })
    .catch(() => { PAPERS[code] = []; papersAsked[code] = false; redrawSubject(code); });
}
const redrawSubject = code => { if (view.code === code) render(); };

// The whole archive in one payload. `needPapers` is per subject on purpose --
// a student opens one course at a time -- but the papers screen is the other
// question, "what is there at all", asked the week before an exam by somebody
// who does not yet know which paper they want. That question cannot be
// answered a subject at a time, so it gets the one fetch it needs.
let ALL_PAPERS = null, allPapersAsked = false;
function needAllPapers() {
  if (ALL_PAPERS || allPapersAsked || !live) return;
  allPapersAsked = true;
  fetch('/papers')
    .then(r => r.ok ? r.json() : Promise.reject(new Error(r.status)))
    .then(d => { ALL_PAPERS = d.papers || []; allPapersAsked = false;
                 redrawPapers(); })
    .catch(() => { ALL_PAPERS = 'failed'; allPapersAsked = false;
                   redrawPapers(); });
}
const redrawPapers = () => { if (view.papers) render(); };
const redrawCampus = () => { if (view.tab === 'campus') render(); };
const redrawCommunity = () => { if (view.tab === 'community') render(); };
const campusList = key => (CAMPUS && CAMPUS[key]) || [];

// Who may change what, in one place. Events are the same bar as adding to the
// library -- a date a hundred and ten people rearrange an afternoon around.
// The directory and the map are the institute's own facts and stay with an
// admin. Nothing here is hidden from anybody: a student sees every screen,
// without the buttons that would 403.
const mayEditEvent = e => atLeast('admin') || (mayAdd() && e && e.mine !== false);
const mayCurate = () => atLeast('admin');

function dateSpan(e) {
  const short = d => MONTHS[+d.slice(5, 7) - 1].slice(0, 3) + ' ' + (+d.slice(8));
  return e.multi ? short(e.date) + ' to ' + short(e.ends) : short(e.date);
}

// A card, not a row: an event carries a date, a society, a venue and a line of
// description, and a row that wraps to four lines is a row pretending to be a
// card.
function eventCard(e) {
  const el = document.createElement('div');
  el.className = 'card' + (e.deleted ? ' gone' : '');
  const h = document.createElement('h3');
  h.textContent = e.title;
  const meta = document.createElement('span');
  meta.className = 'meta';
  meta.textContent = [dateSpan(e), e.society, e.venue].filter(Boolean).join(' · ');
  el.append(h, meta);
  if (e.blurb) {
    const p = document.createElement('p');
    p.textContent = e.blurb;
    el.appendChild(p);
  }
  if (e.deleted) el.appendChild(quiet('Taken down. Only you and the admins see this.'));
  if (mayEditEvent(e)) {
    const acts = document.createElement('div');
    acts.className = 'acts';
    const edit = document.createElement('button');
    edit.textContent = 'Edit';
    edit.onclick = () => go('campus', 'event', e.id);
    const drop = document.createElement('button');
    drop.textContent = e.deleted ? 'Put it back' : 'Take it down';
    drop.onclick = () => saveCampus('/event', {id: e.id, deleted: !e.deleted});
    acts.append(edit, drop);
    el.appendChild(acts);
  }
  return el;
}

function eventsSection() {
  heading('Coming up on campus');
  if (mayAdd()) {
    const add = line('Add an event', 'Everyone approved sees it, here, until '
                     + 'the day it ends', document.createElement('button'));
    add.onclick = () => go('campus', 'event');
    const rows = document.createElement('div');
    rows.className = 'rows';
    rows.appendChild(add);
    nav.appendChild(rows);
  }
  if (!CAMPUS) return void nav.appendChild(waitline('Reading what is on…'));
  if (CAMPUS.failed) {
    return void nav.appendChild(quiet('Events need the server. Run: notes.py serve'));
  }
  const list = campusList('events');
  if (!list.length) {
    return saying('Nothing on the calendar right now.',
      'A fest, a workshop, a competition: anything with a date on it goes '
      + 'here and stays until the day it ends. Nothing is invented: an event '
      + 'is here because somebody in the section put it here.');
  }
  list.forEach(e => nav.appendChild(eventCard(e)));
}

// A club opens where it stands. <details> is the platform's own disclosure --
// no URL, no router, correct for a keyboard and a screen reader before a line
// of this app runs.
function clubCard(c) {
  const el = document.createElement('details');
  el.className = 'card' + (c.hidden ? ' gone' : '');
  const sum = document.createElement('summary');
  const h = document.createElement('h3');
  h.textContent = c.name;
  const meta = document.createElement('span');
  meta.className = 'meta';
  meta.textContent = [c.category, c.hidden ? 'Hidden' : ''].filter(Boolean).join(' · ');
  const words = document.createElement('span');
  words.className = 'nm';
  words.append(h, meta);
  // Twenty-four societies, and not one of them has a logo -- nor should this
  // app invent one. The mark is the society's own initials in the colour its
  // own name produces, which is the same rule people get, so the directory
  // reads as a list of twenty-four things rather than twenty-four chevrons.
  sum.append(face(c.name), words);
  el.appendChild(sum);
  if (c.blurb) {
    const p = document.createElement('p');
    p.textContent = c.blurb;
    el.appendChild(p);
  }
  if (c.tags && c.tags.length) {
    const tags = document.createElement('div');
    tags.className = 'tags';
    c.tags.forEach(t => {
      const s = document.createElement('span');
      s.textContent = t;
      tags.appendChild(s);
    });
    el.appendChild(tags);
  }
  const acts = document.createElement('div');
  acts.className = 'acts';
  if (c.link) {
    const a = document.createElement('a');
    a.href = c.link;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.textContent = 'Their page';
    acts.appendChild(a);
  }
  if (c.contact) acts.appendChild(quiet('Contact: ' + c.contact));
  if (mayCurate()) {
    const edit = document.createElement('button');
    edit.textContent = 'Edit';
    edit.onclick = () => go('campus', 'club', c.slug);
    const hide = document.createElement('button');
    hide.textContent = c.hidden ? 'Show it again' : 'Hide it';
    hide.onclick = () => saveCampus('/club', {slug: c.slug, hidden: !c.hidden});
    acts.append(edit, hide);
  }
  if (acts.childNodes.length) el.appendChild(acts);
  return el;
}

function clubsSection() {
  heading('Clubs and societies');
  if (mayCurate()) {
    const add = line('Add a club', 'Name, what it does, and where to find them',
                     document.createElement('button'));
    add.onclick = () => go('campus', 'club');
    const rows = document.createElement('div');
    rows.className = 'rows';
    rows.appendChild(inked(add));
    nav.appendChild(rows);
  }
  if (!CAMPUS) return void nav.appendChild(waitline('Reading the directory…'));
  if (CAMPUS.failed) {
    return void nav.appendChild(quiet('The directory needs the server. Run: notes.py serve'));
  }
  const list = campusList('clubs');
  if (!list.length) {
    return saying('No societies listed yet.',
      'This is the directory of what runs on campus: what each society does '
      + 'and how to reach them. Your class admin fills it in.');
  }
  list.forEach(c => nav.appendChild(clubCard(c)));
}

// ---- THE MAP. ------------------------------------------------------------
// One adapter per provider, and the page knows nothing else about either. The
// key comes from the server at runtime, never from this file: a static export
// has no server and therefore no key, which is exactly right.
//
// NO key is a state this screen is designed for, not a failure it survives.
// The places list underneath is the useful half and it needs no provider at
// all -- names, what each thing is, and a Directions link that hands off to
// the phone's own maps app.
const MAP_PROVIDERS = {
  google: {
    label: 'Google Maps',
    src: k => 'https://maps.googleapis.com/maps/api/js?key=' + encodeURIComponent(k),
    ready: () => !!(window.google && window.google.maps),
    draw(el, cfg, places) {
      const b = cfg.bounds;
      const box = new google.maps.LatLngBounds(
        {lat: b.south, lng: b.west}, {lat: b.north, lng: b.east});
      const map = new google.maps.Map(el, {
        center: cfg.centre, zoom: 16, mapTypeControl: false,
        streetViewControl: false, fullscreenControl: false,
        // Bounded to the campus, so a dragged finger cannot wander off across
        // Bhopal and strand a first-year looking at a lake.
        restriction: {latLngBounds: box, strictBounds: true},
      });
      map.fitBounds(box);
      places.filter(p => p.lat != null && p.lng != null).forEach(p => {
        new google.maps.Marker({
          map, position: {lat: p.lat, lng: p.lng},
          title: p.name + (p.approx ? ' (approximate)' : ''),
        });
      });
    },
  },
  // Named because it was asked for and because the seam is the point: when a
  // Jio key exists, its SDK's own map-and-marker calls go in draw() below and
  // nothing outside this object changes. Left unwritten rather than guessed --
  // an invented SDK call would look finished and fail on the first real key.
  jio: {
    label: 'Jio Maps',
    src: null,
    ready: () => false,
    draw: null,
  },
};

function mapSection() {
  heading('Finding your way');
  const cfg = (CAMPUS && CAMPUS.maps) || {};
  const prov = MAP_PROVIDERS[cfg.provider];
  if (!CAMPUS || CAMPUS.failed) {
    nav.appendChild(quiet(CAMPUS ? 'The map needs the server. Run: notes.py serve'
                                 : 'Reading the campus…'));
  } else if (!cfg.key) {
    // First-class, and said in words somebody can act on.
    saying('The map needs a key, and this server has none.',
      'Set NEXT_PUBLIC_GOOGLE_MAPS_API_KEY in the environment the server starts in and the '
      + 'map draws here. Everything below works without it.');
  } else if (!prov || !prov.src || !prov.draw) {
    saying('This server names a map provider the app cannot draw yet.',
      'RECARVE_MAPS_PROVIDER is "' + (cfg.provider || '') + '". The adapter for '
      + 'it is in MAP_PROVIDERS and needs its SDK call filled in.');
  } else {
    const box = document.createElement('div');
    box.id = 'campusmap';
    nav.appendChild(box);
    drawMap(prov, cfg, box);
  }
  placesSection();
}

// The provider script is loaded here and only here, and only when a key
// exists: with no key nothing is fetched at all, which is what keeps the app
// working offline and keeps a static export free of anything that phones home.
function drawMap(prov, cfg, box) {
  const places = campusList('places');
  const paint = () => { try { prov.draw(box, cfg, places); } catch (e) {
    box.remove();
    saying('The map did not load.', String(e.message || e));
  } };
  if (prov.ready()) return paint();
  const tag = document.createElement('script');
  tag.src = prov.src(cfg.key);
  tag.async = true;
  tag.onload = paint;
  tag.onerror = () => { box.remove(); saying('The map did not load.',
    'The provider script could not be fetched. The list below still works.'); };
  document.head.appendChild(tag);
}

const PLACE_KINDS = [['academic', 'Where you have class'], ['food', 'Food'],
                     ['hostel', 'Hostels'], ['sport', 'Sport'],
                     ['admin', 'Offices'], ['health', 'Health'],
                     ['gate', 'Gates'], ['other', 'Everything else']];

// Directions with no key and no SDK: the phone's own maps app already knows
// how to get there, and handing it a destination is one link.
function directionsFor(p) {
  const a = document.createElement('a');
  a.href = 'https://www.google.com/maps/dir/?api=1&destination='
         + p.lat + ',' + p.lng;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  a.textContent = 'Directions';
  return a;
}

function placeCard(p) {
  const el = document.createElement('div');
  el.className = 'card' + (p.hidden ? ' gone' : '');
  const h = document.createElement('h3');
  h.textContent = p.name;
  const meta = document.createElement('span');
  meta.className = 'meta';
  // Said out loud on every pin that is one. An approximate pin honestly
  // labelled beats a confident wrong one, and this is where it is labelled.
  meta.textContent = [p.note, p.lat == null ? 'No pin yet'
                      : p.approx ? 'Approximate pin' : ''].filter(Boolean).join(' · ');
  el.append(h, meta);
  const acts = document.createElement('div');
  acts.className = 'acts';
  if (p.lat != null && p.lng != null) acts.appendChild(directionsFor(p));
  if (mayCurate()) {
    const edit = document.createElement('button');
    edit.textContent = 'Edit';
    edit.onclick = () => go('campus', 'place', p.slug);
    const drop = document.createElement('button');
    drop.className = 'row-del';
    drop.textContent = 'Remove';
    drop.onclick = () => saveCampus('/place', {slug: p.slug, remove: true});
    acts.append(edit, drop);
  }
  if (acts.childNodes.length) el.appendChild(acts);
  return el;
}

function placesSection() {
  if (mayCurate()) {
    const add = line('Add a place', 'A name, what it is, and where',
                     document.createElement('button'));
    add.onclick = () => go('campus', 'place');
    const rows = document.createElement('div');
    rows.className = 'rows';
    rows.appendChild(inked(add));
    nav.appendChild(rows);
  }
  if (!CAMPUS || CAMPUS.failed) return;
  const list = campusList('places');
  if (!list.length) {
    return saying('No places listed yet.',
      'Lecture halls, hostels, the canteens, the gates: everywhere a '
      + 'first-year has to find in week one.');
  }
  PLACE_KINDS.forEach(([kind, label]) => {
    const some = list.filter(p => p.kind === kind);
    if (!some.length) return;
    heading(label);
    some.forEach(p => nav.appendChild(placeCard(p)));
  });
}

// ---- The three little forms. --------------------------------------------
// One saver, because it is one shape of request: POST a row, get the whole tab
// back, redraw from what was stored rather than from what was typed.
async function saveCampus(path, payload, err, btn) {
  if (btn) btn.disabled = true;
  busy('Saving…', true);
  try {
    const r = await fetch(path, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || 'could not save that');
    CAMPUS = d;
    busyDone('Saved');
    if (view.compose) history.back(); else render();
  } catch (e) {
    if (btn) btn.disabled = false;
    busyDone('');
    if (err) err.textContent = e.message;
    else busyDone('Could not save that: ' + e.message);
  }
}

// The shared skeleton of all three: labelled fields, an error line, cancel and
// save. Each form below says what its fields are and what to do with them.
function formBox(fields, saveLabel, onSave) {
  const box = document.createElement('div');
  box.className = 'compose';
  const got = {};
  fields.forEach(f => {
    const id = 'f-' + f.name;
    const label = document.createElement('label');
    label.htmlFor = id;
    label.textContent = f.label;
    const el = document.createElement(f.tag || 'input');
    el.id = id;
    if (f.type) el.type = f.type;
    if (f.max) el.maxLength = f.max;
    if (f.placeholder) el.placeholder = f.placeholder;
    if (f.step) el.step = f.step;
    el.value = f.value == null ? '' : String(f.value);
    box.append(label, el);
    got[f.name] = el;
  });
  const err = document.createElement('p');
  err.className = 'err';
  const acts = document.createElement('div');
  acts.className = 'go';
  const cancel = document.createElement('button');
  cancel.textContent = 'Cancel';
  cancel.onclick = () => history.back();
  const save = document.createElement('button');
  save.className = 'primary';
  save.textContent = saveLabel;
  save.onclick = () => onSave(got, err, save);
  acts.append(cancel, save);
  box.append(err, acts);
  nav.appendChild(box);
}

function eventForm() {
  const e = campusList('events').find(x => x.id === view.composeId) || {};
  formBox([
    {name: 'title', label: 'What is it', max: 120, placeholder: 'Vidyut'},
    {name: 'society', label: 'Who is running it', max: 80, placeholder: 'Evolve'},
    // The platform's own date picker, the same one the catch-up screen uses:
    // a typed date is a date typed wrong.
    {name: 'date', label: 'Starts', type: 'date', value: e.date},
    {name: 'ends', label: 'Ends (leave blank if it is one day)', type: 'date',
     value: e.multi ? e.ends : ''},
    {name: 'venue', label: 'Where', max: 120, placeholder: 'MME Auditorium'},
    {name: 'blurb', label: 'What happens', tag: 'textarea', max: 600},
  ].map(f => Object.assign(f, {value: f.value != null ? f.value : e[f.name]})),
  e.id ? 'Save changes' : 'Add it', (got, err, btn) => {
    if (!got.title.value.trim()) return void (err.textContent = 'An event needs a name.');
    if (!got.date.value) return void (err.textContent = 'An event needs a date. That is the whole point of one.');
    saveCampus('/event', {
      id: e.id, title: got.title.value, society: got.society.value,
      date: got.date.value, ends: got.ends.value, venue: got.venue.value,
      blurb: got.blurb.value}, err, btn);
  });
}

function clubForm() {
  const c = campusList('clubs').find(x => x.slug === view.composeId) || {};
  formBox([
    {name: 'name', label: 'Name', max: 80, value: c.name},
    {name: 'category', label: 'Category', max: 40, value: c.category,
     placeholder: 'Technical'},
    {name: 'blurb', label: 'What they do', tag: 'textarea', max: 600, value: c.blurb},
    {name: 'tags', label: 'Tags, separated by commas', max: 200,
     value: (c.tags || []).join(', ')},
    {name: 'link', label: 'Instagram or a page', max: 300, value: c.link,
     placeholder: 'https://instagram.com/…'},
    {name: 'contact', label: 'Who to ask', max: 120, value: c.contact},
  ], c.slug ? 'Save changes' : 'Add it', (got, err, btn) => {
    if (!got.name.value.trim()) return void (err.textContent = 'A club needs a name.');
    saveCampus('/club', {
      slug: c.slug, name: got.name.value, category: got.category.value,
      blurb: got.blurb.value, link: got.link.value, contact: got.contact.value,
      tags: got.tags.value.split(',').map(t => t.trim()).filter(Boolean),
      hidden: !!c.hidden}, err, btn);
  });
}

function placeForm() {
  const p = campusList('places').find(x => x.slug === view.composeId) || {};
  formBox([
    {name: 'name', label: 'Name', max: 80, value: p.name},
    {name: 'kind', label: 'What it is (' + PLACE_KINDS.map(k => k[0]).join(', ') + ')',
     max: 20, value: p.kind || 'academic'},
    {name: 'lat', label: 'Latitude', type: 'number', step: 'any', value: p.lat},
    {name: 'lng', label: 'Longitude', type: 'number', step: 'any', value: p.lng},
    {name: 'note', label: 'Anything worth knowing', max: 200, value: p.note},
  ], p.slug ? 'Save changes' : 'Add it', (got, err, btn) => {
    if (!got.name.value.trim()) return void (err.textContent = 'A place needs a name.');
    saveCampus('/place', {
      slug: p.slug, name: got.name.value, kind: got.kind.value.trim(),
      lat: got.lat.value, lng: got.lng.value, note: got.note.value,
      // Every pin added from a phone is approximate until somebody stands
      // there with it. Nothing on this form claims otherwise.
      approx: true}, err, btn);
  });
}

// Which composer a URL means. Announcements keep 'new' and their own id, which
// is a uuid and can never collide with one of these three words.
// Wrapped rather than named directly: mayAdd is declared further down and a
// bare reference here is read while this object is built, not when a form is
// opened -- which is a temporal dead zone error that blanks the whole app.
const COMPOSERS = {event: [eventForm, () => mayAdd()],
                   club: [clubForm, () => mayCurate()],
                   place: [placeForm, () => mayCurate()]};

// Campus is the physical place and nothing else: what is on, who runs it, and
// where any of it is. The wall, the class board and the notice board used to
// be filed here too, which is how one tab came to hold six unrelated things.
function renderCampus() {
  needCampus();
  const composer = COMPOSERS[view.compose];
  // Writing takes the tab over: it is one thing at a time on a phone, and the
  // lists underneath are not what you are doing.
  if (composer) {
    if (!composer[1]()) return void go('campus');
    return composer[0]();
  }
  // No section named is the whole tab, which is where the thumb bar lands.
  const only = view.sec;
  if (!only || only === 'events') eventsSection();
  if (!only || only === 'clubs') clubsSection();
  if (!only || only === 'places') mapSection();
}

// Community is the people: what the section is saying, what it is asking, and
// what it has put in. Three lists of other students, and none of them is
// printed anywhere else.
async function renderCommunity() {
  const mine = painted;
  // Writing takes the tab over, the way it already does on Campus: the screen
  // opens on what the section has said, and the + is how you say something.
  // A composer sitting on top of the feed meant this tab opened on an empty
  // form -- work -- rather than on the thing anybody came here to read.
  if (view.compose) {
    if (!WALL_COMPOSE[view.compose]) return void go('community');
    wallOn = WALL_COMPOSE[view.compose];
    const box = document.createElement('div');
    box.className = 'wall';
    box.appendChild(wallComposer(wallOn));
    nav.appendChild(box);
    return;
  }
  const only = view.sec;
  if (!only || only === 'board') wallSection();
  if (!only || only === 'doubts') doubtsSection();
  if (only && only !== 'standings') return;
  heading('Who has contributed');
  windowPicker();
  const box = document.createElement('div');
  box.className = 'mine';
  nav.appendChild(box);
  if (!BOARD) {
    waiting(box, 'Reading the class board…');
    try {
      const r = await fetch('/standings');
      if (!r.ok) throw new Error();
      const d = await r.json();
      if (mine !== painted) return;
      BOARD = d;
    } catch (e) {
      if (mine !== painted) return;
      failed(box, 'The class board needs the server.',
             'Nothing answered. If this is the Mac that runs the class library, '
             + 'start it with: notes.py serve');
      return;
    }
  }
  drawBoard(box);
}

// ---- THE DAY VIEW. The question the subject list cannot answer. --------
// A student does not ask "what is in Chemistry", they ask "what have I got
// today" -- and then "what did I have last Tuesday, when I was asleep". This
// is that screen: one day, its periods in order, and against each period
// everything the app already knows about it.
//
// It invents nothing and it recomputes nothing. The periods are the student's
// OWN timetable, which is why the labs come out right: Section I splits by
// batch, the section template is only what a profile is seeded from, and the
// per-profile copy is the one that says which lab this student sits in.
// classRow marks the period -- the same control, on the same payload, and the
// arithmetic stays where it has always been, on the server.

// Which day a lecture is the record of. Its own name when that carries a date
// -- audio comes off a phone named for the class it was -- and otherwise the
// day its notes landed, which is already what Home treats as a note's date.
const noteDay = n => (/[0-9]{4}-[0-9]{2}-[0-9]{2}/.exec(n.title) || [])[0]
                     || (n.at ? isoDay(new Date(n.at * 1000)) : null);

function renderDay() {
  const date = dayDate || attToday();
  const day = dayOfISO(date);
  // No min and no max: the timetable knows what a Tuesday holds whichever
  // Tuesday it is, and a day too old or too far ahead to mark simply loses its
  // buttons -- classRow decides that, in the one place that decides it.
  dayPicker(date, d => { dayDate = d; render(); });
  if (TT === null) return block(dayName(date), [waitline('Checking your timetable…')]);
  if (!TT.length) {
    if (!live) {
      return block(dayName(date),
        [quiet('Your timetable needs the server. Run: notes.py serve')]);
    }
    return block(dayName(date), [quiet('Your section\u2019s week has not been set up yet. A class rep or an admin sets it once, for everybody.')]);
  }
  const slots = slotsFor(date);
  const rows = [];
  const shelved = new Set();
  for (const slot of slots) {
    rows.push(classRow(date, slot, mayAdd()));
    const s = subjectOf(slot.code);
    // Every period gets its own row and its own marking. What hangs off it is
    // per SUBJECT, so a subject twice in one day gets it once: one recording
    // was one class, and printing it under both periods claims it was both.
    if (!s || shelved.has(slot.code)) continue;   // or a code the library dropped
    shelved.add(slot.code);
    // What was recorded in THIS slot: the lectures of that subject that carry
    // this date. Nothing is filed by period, so nothing here pretends to be.
    for (const n of lecturesOf(s)) {
      if (noteDay(n) === date) rows.push(noteRow(n, s));
    }
    // And the rest of what the subject holds. A count and a way in, not the
    // whole shelf: the shelf is one tap away and it is already a screen.
    if (s.uploads.length) {
      const b = line('Notes & slides in ' + s.code,
                     plural(s.uploads.length, 'file') + ' under this subject',
                     document.createElement('button'), hue(slot.code));
      b.onclick = () => go('classes', slot.code);
      rows.push(b);
    }
  }
  if (!rows.length) rows.push(emptyDay(date, day));
  // One tap for the whole day, on the days it can save one. Same row the
  // catch-up screen offers, over the same classes, posting the same request.
  const all = markable(date) ? allPresentRow(date, slots) : null;
  if (all) rows.push(all);
  blockWithTimeline(dayName(date), date, rows);
}

// LEVEL 1: every subject, empty ones included. Nobody can add a chemistry
// recording to a subject the app never told them was there.
function renderSubjects() {
  // The way into the day view, saying what today holds before it is tapped.
  const n = TT ? slotsFor(attToday()).length : 0;
  const today = line('Today\u2019s classes',
    TT === null ? 'Checking your timetable…'
    : n ? n + (n === 1 ? ' class' : ' classes') + ' · and any other day'
    // Name the holiday rather than the weekday: "Nothing on Monday" under a
    // timetable full of Mondays reads as a fault in the app.
    : (closedOn(attToday()) || {}).title
      ? (closedOn(attToday()).title + ' · step back to any day')
      : 'Nothing on ' + DAYS[dayOfISO(attToday())] + ' · step back to any day',
    document.createElement('button'));
  today.onclick = () => go('classes', 'day');
  // The three screens that are about your week rather than about a subject.
  // They were reached from Home, which made the week a thing you left Home to
  // do and then could not find again; they live on the tab that holds the
  // subjects they are a timetable OF.
  const week = [today];
  if (ATT) {
    const mark = line('Your attendance', 'Per subject, and a day you forgot to mark',
                      document.createElement('button'));
    mark.onclick = () => go('classes', 'attendance');
    week.push(mark);
  }
  block('Your week', week);
  // The institute's calendar, and what is next on it. Home shows today; this
  // is the tab that holds the week around it.
  if (ATT) calendarWeek();
  comingUpBlock();

  // One run over the whole library, because the week before an exam is not
  // spent one subject at a time. What is due comes first, so tapping this
  // repeatedly is a revision plan and not a re-read.
  const every = allItems();
  if (every.length) {
    const exam = examSoon();
    const b = line('Practice everything',
                   exam ? exam + ' \u00b7 ' + srSays(srStats(every)) : srSays(srStats(every)),
                   document.createElement('button'));
    b.onclick = qOpenAll;
    block('Practice', [b]);
  }

  block('Subjects', DATA.map(s => {
    const b = document.createElement('button');
    b.className = 'row';
    b.style.setProperty('--h', hue(s.code));
    b.innerHTML = '<i class="tick"></i><span class="name"><b></b><small></small></span>'
                + '<span class="code"></span>';
    b.querySelector('b').textContent = s.name;
    b.querySelector('small').textContent = counts(s);
    b.querySelector('.code').textContent = s.code;
    b.onclick = () => go('classes', s.code);
    return b;
  }));

  // What landed most recently, across the whole library. The list above it is
  // subjects and answers "where is X"; this is lectures, newest first, and
  // answers the other thing somebody opens this screen for -- "what has been
  // added since I last looked". It used to live in the empty reading pane on
  // the wide layout, which is a pane that no longer exists.
  const latest = DATA.flatMap(s => s.notes.map(n => ({n: n, s: s})))
    .filter(x => x.n.at).sort((a, b) => b.n.at - a.n.at).slice(0, 4);
  block('Added most recently', latest.map(x => {
    const day = noteDay(x.n);
    const r = line(x.n.title,
                   day ? (+day.slice(8)) + ' ' + MONTHS[+day.slice(5, 7) - 1] : x.s.name,
                   document.createElement('button'), hue(x.s.code));
    r.appendChild(chip(x.s.code));
    r.onclick = () => go('classes', x.s.code, x.n.title);
    return r;
  }));
}

// LEVEL 2: one subject, grouped.
function renderSubject(s) {
  // Where you stand in THIS subject, at the top, because 75% is per subject
  // and this is the screen you open when the subject is what you are worried
  // about. attended / held, the true percentage, and the one sentence that
  // says what to do about it.
  const a = attOf(s.code);
  if (a) {
    const mark = line('Mark your attendance', 'Today, or a day you forgot',
                      document.createElement('button'));
    mark.onclick = () => go('classes', 'attendance');
    block('Attendance', [attRow(a, false), mark]);
  }
  // Revising a whole course, not one lecture: every note's questions in one run.
  const all = quizItems(s, null);
  if (all.length) {
    const b = document.createElement('button');
    b.className = 'row';
    b.style.setProperty('--h', hue(s.code));
    b.innerHTML = '<i class="tick"></i><span class="name"><b></b><small></small></span>';
    b.querySelector('b').textContent = 'Practice the whole course';
    b.querySelector('small').textContent = srSays(srStats(all));
    b.onclick = () => qOpen(s, null);
    block('Practice', [b]);
  }
  block('Lectures', lecturesOf(s).map(n => noteRow(n, s)));
  block('Notes & slides', groupedFileRows(s));
  papersSection(s);
  const rev = revisionOf(s);
  block('Revision sheet', rev ? [noteRow(rev, s)] : []);
  roomSection(s);
  if (!s.notes.length && !s.uploads.length) {
    saying('Nothing in ' + s.code + ' yet.',
           'Tap + to record a class or to add slides you already have. It files '
           + 'itself here, and the notes come back when the Mac has made them.');
  }
}

// ---- THE CLASS ROOM: one per subject, and only while you are looking. ----
//
// Short messages, oldest at the top, newest at the bottom, which is how a
// conversation is read. Any approved member reads and writes it; the author
// or an admin takes a message down and it is hidden, never destroyed, like
// everything else somebody said out loud in this app.
//
// The polling is the whole design and it is deliberately small. No socket, no
// broker, no dependency: the page already polls /jobs, and this is one more
// interval that runs ONLY while the room is on the screen. It never asks for
// the history -- `roomLast` is the last id this phone holds and the request
// is for what came after it, so the usual answer is an empty list. A hidden
// tab stops asking entirely, because a phone in a pocket is not reading.
const ROOM_POLL = 5000;
let roomOpen = null;    // the subject code whose room is expanded, or null
let roomMsgs = [];
let roomRead = false;   // has the first poll come back? an unasked room is
                        // not an empty one, and must not be drawn as one
let roomLast = 0;
let roomTimer = null;
let roomDead = false;   // a static export has no server to talk to

// Somebody's typing, put on the page as typing. textContent, never innerHTML:
// this is the other screen where a tag is four words that look like a tag.
function chatLine(m) {
  const el = document.createElement('div');
  el.className = 'msg' + (m.mine ? ' me' : '');
  const said = document.createElement('p');
  said.className = 'said';
  said.textContent = m.body;
  const meta = document.createElement('p');
  meta.className = 'meta';
  meta.textContent = m.by + ' · ' + ago(m.at, NOW);
  el.append(said, meta);
  if (m.mine || atLeast('admin')) {
    const drop = document.createElement('button');
    drop.textContent = m.mine ? 'Delete' : 'Remove';
    if (!m.mine) drop.className = 'adm';
    drop.onclick = () => sayInRoom({id: m.id, delete: true}, drop);
    el.appendChild(acts(drop));
  }
  return el;
}

function drawRoom(log) {
  log.innerHTML = '';
  if (roomDead) {
    return log.appendChild(quiet('The room needs the server. Run: notes.py serve'));
  }
  if (!roomRead) return log.appendChild(waitline('Opening the room…'));
  if (!roomMsgs.length) {
    return log.appendChild(quiet('Nothing said in here yet. Ask about the '
      + 'homework, or say where the lab moved to.'));
  }
  roomMsgs.forEach(m => log.appendChild(chatLine(m)));
  log.scrollTop = log.scrollHeight;     // newest at the bottom, in view
}

// One place merges what came back, whether it came from a poll or from
// sending: the server always answers with what is after `since`, so this is
// an append and never a replace -- and `removed` is the one message that has
// to go the other way.
function roomTook(d) {
  roomRead = true;
  if (d.removed) roomMsgs = roomMsgs.filter(m => m.id !== d.removed);
  for (const m of (d.messages || [])) {
    roomMsgs.push(m);
    if (m.id > roomLast) roomLast = m.id;
  }
}

async function pollRoom(code, log) {
  // The element is gone the moment render() repaints the tab, and that is the
  // signal to stop: no listener to remove, nothing to leak, and a room that
  // cannot keep polling after you have walked away from it.
  if (!document.body.contains(log) || roomOpen !== code) {
    clearInterval(roomTimer);
    roomTimer = null;
    return;
  }
  if (document.visibilityState === 'hidden') return;   // a pocket is not reading
  try {
    const r = await fetch('/chat?subject=' + encodeURIComponent(code)
                        + '&since=' + roomLast);
    if (!r.ok) return;
    const d = await r.json();
    if (roomOpen !== code || !d.messages || !d.messages.length) return;
    roomTook(d);
    drawRoom(log);
  } catch {
    // One missed poll is not worth a sentence; never having reached the
    // server at all is, because the box below it cannot work either.
    if (!roomLast) { roomDead = true; drawRoom(log); }
  }
}

async function sayInRoom(payload, btn) {
  const code = roomOpen;
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/chat', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign({subject: code, since: roomLast}, payload)),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || 'could not send that');
    if (roomOpen !== code) return;
    roomTook(d);
    render();
  } catch (e) {
    if (btn) btn.disabled = false;
    busyDone(e.message);
  }
}

function roomSection(s) {
  heading('Class room');
  if (roomOpen !== s.code) {
    const open = line('Open the ' + s.code + ' room',
                      'Ask the class something, right now',
                      document.createElement('button'));
    open.onclick = () => {
      roomOpen = s.code;
      roomMsgs = [];
      roomLast = 0;
      roomRead = false;
      roomDead = false;
      render();
    };
    nav.appendChild(open);
    return;
  }
  const box = document.createElement('div');
  box.className = 'room';
  const log = document.createElement('div');
  log.className = 'log';
  box.appendChild(log);
  drawRoom(log);

  const say = document.createElement('div');
  say.className = 'saybox';
  const input = document.createElement('input');
  input.maxLength = 500;
  input.placeholder = 'Say something to ' + s.code;
  input.setAttribute('aria-label', 'Your message');
  const send = document.createElement('button');
  send.textContent = 'Send';
  const fire = () => {
    const body = input.value.trim();
    if (!body) return;
    input.value = '';
    sayInRoom({body: body}, send);
  };
  send.onclick = fire;
  input.onkeydown = e => { if (e.key === 'Enter') fire(); };
  say.append(input, send);
  box.appendChild(say);

  const shut = document.createElement('button');
  shut.className = 'cmt';
  shut.textContent = 'Close the room';
  shut.onclick = () => { roomOpen = null; render(); };
  box.appendChild(acts(shut));
  nav.appendChild(box);

  if (!roomTimer) roomTimer = setInterval(() => pollRoom(s.code, log), ROOM_POLL);
  // The first look at a room asks with no `since`, which is the only request
  // that ever brings the tail back.
  if (!roomMsgs.length && !roomLast) pollRoom(s.code, log);
}

// THE ME TAB: who you are, what your role lets you do, and what you have put
// in. Plus -- if you run the class library -- the way into the admin panel.
//
// Points are status and nothing else. Nothing in this app asks for a score
// before it shows you something, and no lock on this page is opened by one:
// the locks are roles, and the only thing that moves a role is an admin.
const ROLE_TITLE = {student: 'Student', trusted: 'Trusted member',
                    cr: 'Class representative', admin: 'Admin'};
const ROLE_SAYS = {
  student: 'Read everything the class has, search it, practise from it, and '
         + 'upvote the notes that helped.',
  trusted: 'Everything a student can, and add notes and slides, record a class, '
         + 'build a revision sheet, and use Explain.',
  cr: 'Everything a trusted member can, and put a notice on the board that '
    + 'every approved classmate sees.',
  admin: 'Everything a class representative can, and let people in, set what '
       + 'each of them may do, and hold the invite code.',
};
// Said in exactly one place, and read by the sheet, the Explain panel and this
// screen. A lock that gives three different reasons is three locks.
const LOCK_ADD = 'Adding to the library is for trusted members. An admin makes '
  + 'you one. Ask in your class group and say what you want to add.';
// What each action is worth, in the order the tally above shows them. The
// numbers are the view's -- change them there and change them here, and the
// test that reads both is what says so.
const POINT_RULES = [
  ['uploads', 5, 'Notes or slides you add'],
  ['recordings', 10, 'A class you record'],
  ['votes_received', 1, 'An upvote on something you added'],
];
const LOCK_EXPLAIN = 'Explain is for trusted members: every tap spends the '
  + 'class’s API budget on the Mac that runs this. An admin makes you '
  + 'trusted. Ask in your class group.';

// Your own two fields, in the card they replace. Nothing else on this screen
// is yours to change: /profile writes name and phone, and the database pins
// status and role to what they already are whatever the request asks for.
function profileCard(box, d) {
  box.className = 'mine';
  box.innerHTML = '<div class="score"></div><p class="sub"></p>'
                + '<div><span class="badge"></span></div>';
  // Your own, at the size that makes it yours rather than a row's mark -- and
  // in the colour every other screen will draw you in, which is what makes it
  // recognisable as you and not a decoration on a profile card.
  if (d.name) box.insertBefore(face(d.name, 'xl'), box.firstChild);
  box.querySelector('.score').textContent = d.name || 'You';
  // Branch and year sit with the roll number because they are the same kind of
  // fact -- what the institute says you are -- and none of them is editable.
  // Absent for anybody the roll list has never heard of, and the line simply
  // gets shorter rather than growing an "unknown".
  const ORDINAL = {1: '1st', 2: '2nd', 3: '3rd'};
  box.querySelector('.sub').textContent =
    [d.roll_no, d.branch, d.year ? (ORDINAL[d.year] || d.year + 'th') + ' year' : '',
     d.section, d.phone].filter(Boolean).join(' · ');
  const badge = box.querySelector('.badge');
  badge.className = 'badge ' + (d.role || 'student');
  badge.textContent = ROLE_TITLE[d.role] || 'Student';
  const edit = document.createElement('button');
  edit.className = 'edit';
  edit.textContent = 'Edit your name or number';
  edit.onclick = () => editProfile(box, d);
  box.appendChild(edit);
}

function editProfile(box, d) {
  box.className = 'mine';
  box.innerHTML = '<div class="score">Your details</div>'
    + '<div class="pform"><label for="pname">Name</label>'
    + '<input id="pname" autocomplete="name">'
    + '<label for="pphone">Phone number</label>'
    + '<input id="pphone" type="tel" inputmode="tel" autocomplete="tel">'
    + '<p class="err" id="perr"></p>'
    + '<div class="go"><button id="pcancel">Cancel</button>'
    + '<button id="psave" class="primary">Save</button></div></div>';
  const name = box.querySelector('#pname'), phone = box.querySelector('#pphone');
  const err = box.querySelector('#perr');
  name.value = d.name || '';
  phone.value = d.phone || '';
  box.querySelector('#pcancel').onclick = () => profileCard(box, d);
  box.querySelector('#psave').onclick = async () => {
    err.textContent = '';
    try {
      const r = await fetch('/profile', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: name.value, phone: phone.value}),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || 'could not save that');
      // The server's answer, not what was typed: it normalises the number, and
      // showing the typed one would say a different thing is stored.
      d.name = j.name; d.phone = j.phone;
      // Rename yourself and the face in the header follows in the same tap --
      // its letters and its colour are both made of the name.
      drawMyFace(j.name);
      profileCard(box, d);
      busyDone('Saved');
    } catch (e) {
      err.textContent = e.message;
    }
  };
}

// ---- YOU. Three screens, reached from the avatar in the header rather than
// from the tab bar: who you are, what you saved, and what you have put in.
// Three rather than one because the one was a scroll -- a profile, a role, a
// score, the rules behind the score, and two lists of your own files, all on
// a screen somebody opens to copy an invite code.
async function renderMe() {
  // What you saved needs nothing from the server: the bookmarks rode in on the
  // /data the page already fetched.
  if (view.me === 'saved') return savedScreen();
  // Before the fetch, not after: About is constants, and a screen that could
  // have drawn offline should not fail behind "your profile needs the server".
  if (view.me === 'about') return aboutScreen();
  const mine = painted;
  const box = document.createElement('div');
  box.className = 'mine';
  nav.appendChild(box);
  waiting(box, 'Reading your profile…');
  let d;
  try {
    const r = await fetch('/me');
    if (!r.ok) throw new Error();
    d = await r.json();
  } catch (e) {
    failed(box, 'Your profile needs the server.',
           'Nothing answered. If this is the Mac that runs the class library, '
           + 'start it with: notes.py serve');
    return;
  }
  // Not just "are we still on this tab": a second paint of this same tab
  // replaced everything below, and appending to it now would double it.
  if (mine !== painted) return;
  // This screen asked /me for its own reasons; the header may still be on the
  // drawn glyph because its own request has not landed, or was made before
  // anybody signed in. Free.
  drawMyFace(d.name);
  if (view.me === 'points') return pointsScreen(box, d);
  profileCard(box, d);

  // What the role actually permits, said once, on the screen somebody comes to
  // after a control elsewhere told them it was not theirs. A student is not
  // told off for being one: they are told what they have and what opens more.
  const can = [line(ROLE_TITLE[d.role] || 'Student',
                    ROLE_SAYS[d.role] || ROLE_SAYS.student)];
  // Asked of the row the server sent, not of ROLE, because this card is drawn
  // from /me -- but asked as a rung either way, so a role added above student
  // is not quietly told it cannot add.
  if (ROLES.indexOf(d.role) < ROLES.indexOf('trusted'))
    can.push(line('What trusted adds', LOCK_ADD));
  block('Your access', can);

  // The things only an admin can do, on the only screen that is theirs, marked
  // as theirs. The server decides: a member's /me carries no invite code at
  // all, and the invites table has no read policy for them either.
  if (d.admin) {
    const rows = [];
    const a = document.createElement('a');
    a.href = '/admin';
    rows.push(inked(line('Class admin',
      d.pending ? (d.pending === 1 ? 'One person is waiting to be let in'
                                   : d.pending + ' people are waiting to be let in')
                : 'Members, roles, the invite link, and anything reported', a)));
    if (d.invite) {
      const b = line(d.invite, 'Invite code, tap to copy',
                     document.createElement('button'));
      const cap = b.querySelector('small');
      b.onclick = async () => {
        try { await navigator.clipboard.writeText(d.invite); flash(cap, 'Copied'); }
        catch { flash(cap, 'Copy failed. Read it out'); }
      };
      rows.push(inked(b));
    } else {
      rows.push(inked(line('No invite code is live',
                           'Nobody can join until there is one')));
    }
    block('Admin', rows);
  }

  const out = line('Log out', 'On this phone only', document.createElement('button'));
  out.onclick = async () => {
    try { await fetch('/logout', {method: 'POST'}); } catch {}
    // The offline copies are this person's library; the next one to sign in
    // on this phone should not be reading it.
    try { (await caches.keys()).forEach(k => caches.delete(k)); } catch {}
    location.href = '/';
  };
  block('This device', [out]);
}

// The score, the rules that made it, and the two lists of what you put in. The
// class board -- everybody else's score -- is on Community, because that is a
// list of other people and this is a fact about you.
function pointsScreen(tally, d) {
  const p = d.points;
  tally.innerHTML = '<div class="score"></div><p></p>'
    + '<div class="tally"><div><b class="u"></b>uploads</div>'
    + '<div><b class="r"></b>recordings</div><div><b class="v"></b>votes received</div></div>'
    + '<div><span class="badge lvl"></span></div>';
  tally.querySelector('.score').textContent = p.score + (p.score === 1 ? ' point' : ' points');
  // The level, and the next one, on the card that carries the number they are
  // both made of. Nothing here is awarded: change the score and the word
  // follows it the same second.
  const up = nextLevel(p.score);
  tally.querySelector('p').textContent =
    'Points are a thank-you, not a key. Everything in the library is open to everyone.'
    + (up ? ' ' + up[1] + ' at ' + plural(up[0], 'point') + '.' : '');
  tally.querySelector('.u').textContent = p.uploads;
  tally.querySelector('.r').textContent = p.recordings;
  tally.querySelector('.v').textContent = p.votes_received;
  tally.querySelector('.lvl').textContent = levelOf(p.score)[1];

  // The rules, in full, on the screen that shows the score they produced.
  // Nobody should have to guess why they have the number they have -- so each
  // row is the action, what it is worth, and what this person has earned from
  // it. The weights are the database's (0021_standings.sql); these three lines
  // are what they mean.
  block('How points work', POINT_RULES.map(([key, weight, what]) => line(
    what,
    plural(weight, 'point') + ' each · '
      + (p[key] ? p[key] + ' × ' + weight + ' = ' + plural(p[key] * weight, 'point')
                : 'nothing from this yet'))));
  nav.appendChild(quiet('A recording counts once its notes are made, and a file '
    + 'once it is visible to the class. Ties share a place on the board.'));

  block('Notes & slides you added', d.uploads.map(u => line(
    u.name, u.subject + ' · ' + plural(u.votes, 'vote')
            + (u.status === 'visible' ? '' : ' · ' + u.status))));
  block('Classes you recorded', d.recordings.map(r => line(
    r.title, r.subject + ' · ' + (r.status === 'done' ? 'notes ready' : r.status))));
  if (!d.uploads.length && !d.recordings.length) {
    saying('Nothing from you yet.',
           d.role === 'student'
             ? 'Trusted members add the notes and the recordings. Everything '
               + 'they add is yours to read, search, practise from and upvote.'
             : 'Tap + to record a class or add your slides. Whatever you add '
               + 'shows up here, with what the class made of it.');
  }
}

// Search cuts across the levels: it answers from every subject whatever screen
// you typed it on, and a hit goes straight to the note.
function renderSearch(needle) {
  let shown = 0;
  for (const s of DATA) {
    const subjHit = (s.code + ' ' + s.name).toLowerCase().includes(needle);
    const notes = s.notes.filter(n => subjHit
      || n.title.toLowerCase().includes(needle) || n.md.toLowerCase().includes(needle));
    const files = s.uploads.filter(u => subjHit || u.name.toLowerCase().includes(needle));
    if (!notes.length && !files.length) continue;
    shown += notes.length + files.length;

    const g = document.createElement('div');
    g.className = 'group';
    g.style.setProperty('--h', hue(s.code));
    g.innerHTML = '<span class="code"></span><h2></h2>';
    g.querySelector('.code').textContent = s.code;
    g.querySelector('h2').textContent = s.name;

    const rows = document.createElement('div');
    rows.className = 'rows';
    notes.forEach(n => rows.appendChild(noteRow(n, s)));
    files.forEach(u => rows.appendChild(fileRow(u, s)));
    nav.append(g, rows);
  }
  if (!shown) saying('Nothing matches that. Try a subject code like CY1107.',
                     'This reads every note and every transcript in the library, '
                     + 'not only their titles, so a word you remember the '
                     + 'professor saying will find the lecture it was said in.');
}

// Bumped by every paint. renderMe is the one screen that has to wait on a
// request before it can draw, so it is the one that can come back to a screen
// somebody else has already redrawn -- and appending into that one gave an
// admin two of every row. The tab is opened twice on the way in as a matter of
// course: once by the router, once when /data answers.
let painted = 0;

function render() {
  painted++;
  // Walking anywhere at all puts the drawer away; the rail, which is the same
  // element on a wide screen, is never 'drawered' and so is never closed.
  closeDrawer();
  paintDrawer();
  const s = view.code ? subjectOf(view.code) : null;
  const searchable = view.tab === 'classes' && !view.att && !view.day
                    && !view.papers;
  nav.innerHTML = '';
  // Home keeps the brand; every other screen names itself in the same header,
  // and #lback -- the only back button at this depth -- appears only where
  // there is a level above to climb to.
  brand.hidden = view.tab !== 'home' || !!view.compose;
  shead.hidden = view.tab === 'home' && !view.compose;
  lback.hidden = !s && !view.att && !view.compose && !view.day
                 && !view.me && !view.papers;
  scode.hidden = !s;
  // Search is the subject list's own tool. It must not answer over Community,
  // and it must not answer over the three screens inside Classes that are not
  // lists of notes -- where a word left in the box hijacked the whole screen.
  q.hidden = !searchable;
  const needle = searchable ? q.value.trim().toLowerCase() : '';
  tools.hidden = view.tab !== 'me' || !!view.me;
  // Home shows the same jobs under "Needs you"; two copies of a running
  // transcription on one screen is one copy too many.
  jobsBox.hidden = view.tab === 'home';
  // The one back button at this depth serves several levels now, so it has to
  // say which one it climbs to.
  const up = view.compose && view.tab === 'community'
      ? ['‹ Community', 'Back to the section']
    : view.compose && !COMPOSERS[view.compose] ? ['‹ Home', 'Back to the notice board']
    : view.compose ? ['‹ Campus', 'Back to Campus']
    : view.me ? ['‹ You', 'Back to your profile']
    : ['‹ Subjects', 'Back to all subjects'];
  lback.textContent = up[0];
  lback.setAttribute('aria-label', up[1]);
  if (s) {
    scode.textContent = s.code;
    scode.style.setProperty('--h', hue(s.code));
  }
  sname.textContent = s ? s.name
    : view.papers ? 'Past papers'
    : view.day ? 'Your day'
    : view.att ? 'Your attendance'
    : view.tab === 'community' && view.compose
      ? (view.compose === 'confess' ? 'Anonymous' : 'New post')
    // Campus has three composers of its own, and they used to borrow the
    // notice board's words: adding a club said "Edit notice" over it.
    : COMPOSERS[view.compose] ? (view.composeId ? 'Edit ' : 'Add ') + view.compose
    : view.compose ? (view.compose === 'new' ? 'New notice' : 'Edit notice')
    : view.me === 'saved' ? 'Saved'
    : view.me === 'points' ? 'Your contributions'
    : TAB_TITLE[view.tab];
  tabBtns.forEach(b => {
    if (b.dataset.tab === view.tab) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  paintFab();   // the + means the composer on Community and the sheet elsewhere
  if (needle) renderSearch(needle);
  else if (view.att) renderAttendance();
  else if (view.day) renderDay();
  else if (view.papers) renderPapers();
  else if (view.tab === 'home') renderHome();
  else if (view.tab === 'campus') renderCampus();
  else if (view.tab === 'community') renderCommunity();
  else if (view.tab === 'me') renderMe();
  else if (s) renderSubject(s);
  else renderSubjects();
}

// The router. One place decides which tab, and which level inside it, you are
// looking at.
function route() {
  // Back out of practice first: the quiz is not a URL, so without this the
  // Android back gesture would leave the app from underneath an open quiz.
  if (quiz.hidden === false) quiz.hidden = true;
  const parts =
    location.hash.slice(1).split('/').filter(Boolean).map(decodeURIComponent);
  // Links shared before the tabs existed: '#CY1107', '#CY1107/note', and the
  // oldest form of all, '#<note title>'. Point them at the note and upgrade
  // the URL in place, exactly as the two-level form already did.
  if (parts.length && !TABS.includes(parts[0])) {
    const s = subjectOf(parts[0]);
    const owner = s ? parts[0]
      : (DATA.find(x => x.notes.some(n => n.title === parts[0])) || {}).code;
    if (owner) {
      history.replaceState(null, '', hashOf('classes', owner, s ? parts[1] : parts[0]));
      return route();
    }
  }
  // A level that changed tabs. Rewritten in place and re-routed, so the screen
  // it named still opens and the bar shows where it lives now. An
  // announcement's own id is a uuid and cannot collide with a composer's word,
  // which is what lets '#campus/<uuid>' be recognised as one of these.
  const moved = MOVED[parts.slice(0, 2).join('/')]
    || (parts[0] === 'campus' && parts[1] && !COMPOSERS[parts[1]]
        && !sectionOf('campus', parts[1])
        ? 'home/' + parts[1] : null);
  if (moved) {
    history.replaceState(null, '',
                         hashOf(...moved.split('/'), ...parts.slice(2)));
    return route();
  }
  const tab = TABS.includes(parts[0]) ? parts[0] : 'home';
  const s = tab === 'classes' ? subjectOf(parts[1]) : null;
  const title = s ? parts[2] : null;
  // Catching up on attendance is a level inside Classes and therefore a URL,
  // so back climbs out of it exactly like every other step.
  const att = tab === 'classes' && parts[1] === 'attendance';
  // The day view is the same shape one tab over: a level inside Classes, so
  // back climbs to the subject list rather than out of the app. 'day' can
  // never collide with a subject -- every code is letters and digits.
  const dayv = tab === 'classes' && parts[1] === 'day';
  // The archive, across every course. A level inside Classes for the same
  // reason the three above are: back climbs to the subject list, and the
  // screen has a URL somebody can send. 'papers' cannot collide with a
  // subject code -- every code carries digits.
  const papersv = tab === 'classes' && parts[1] === 'papers';
  // So is the composer, for the same reason: without a URL the back gesture
  // dropped what was typed and left the tab stuck on an empty form. Two tabs
  // have one now -- an event, a club or a place on Campus, and a notice on
  // Home, which is where the notice board lives.
  // Community has two of its own -- 'say' and 'confess' -- for the same
  // reason: the + opens a form, and a form with no URL loses what was typed
  // to the back gesture.
  // Which list inside the tab, when it is one of them rather than the whole
  // screen. Read before the composer, because both are parts[1] and a section
  // word is never a composer word.
  const sec = sectionOf(tab, parts[1]);
  const compose = !sec && (tab === 'campus' || tab === 'home' || tab === 'community')
    ? parts[1] || null : null;
  // And the row being edited, when there is one: '#campus/club/robotics'. The
  // announcement composer carries its id in `compose` itself, which is a uuid
  // and can never be mistaken for one of the three words above.
  const composeId = compose ? parts[2] || null : null;
  // Which of your own screens: the profile, what you saved, or what you have
  // put in. One level under '#me', so '#me' on its own still lands somewhere.
  const me = tab === 'me' ? parts[1] || null : null;
  const was = view;
  view = {tab, code: s ? s.code : null, title: title || null, att, compose,
          composeId, day: dayv, papers: papersv, me, sec};
  // Not every render, and not every keystroke inside one: only a step to a
  // different tab, a different subject or a different level of one.
  const moving = !was || was.tab !== view.tab || was.code !== view.code
    || was.title !== view.title || was.att !== view.att
    || was.day !== view.day || was.me !== view.me || was.compose !== view.compose
    || was.papers !== view.papers || was.sec !== view.sec;
  if (!att) attDate = null;     // and re-opening it starts on today, not last week
  if (!dayv) dayDate = null;    // today by default, every time it is opened
  const n = s && title ? s.notes.find(x => x.title === title) : null;
  if (n) openNote(n, s); else closeRead();
  render();
  if (moving) arrive(n ? document.getElementById('read') : nav);
}

// 180ms of the new screen coming up six pixels, so a tab change is something
// you watched happen rather than something that had already happened. The
// class comes straight back off: it is re-added by the next route(), and a
// class left on would make the animation fire again the next time the
// element is shown.
function arrive(el) {
  // A new screen starts at the top, so the + starts back on it.
  document.body.classList.remove('fabaway');
  if (!el) return;
  el.classList.remove('swap');
  void el.offsetWidth;            // restart it even on a repeat of the same step
  el.classList.add('swap');
  el.addEventListener('animationend', () => el.classList.remove('swap'),
                      {once: true});
}

// Markdown in, typeset HTML out. The note, the Explain panel and a practice
// question all wanted this and each had grown its own copy of the delimiters.
function mdInto(el, md) {
  el.innerHTML = marked.parse(md || '');
  typeset(el);
}

// KaTeX is two deferred scripts, and a deferred script runs AFTER the inline
// one this page is -- so opening a note by reloading on it, or by following a
// link straight into it, typeset nothing and printed the LaTeX: "$\det(A -
// \lambda I) = 0$" in the middle of a maths lecture. Walking in from the
// subject list hid it, because by then the scripts had landed. So the one
// case that arrives too early waits for the document and does it again.
function typeset(el) {
  if (!window.renderMathInElement) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => typeset(el), {once: true});
    }
    return;     // anything later than that means KaTeX is not coming at all
  }
  renderMathInElement(el, {
    delimiters: [
      {left:'$$', right:'$$', display:true},
      {left:'$', right:'$', display:false},
      {left:'\\(', right:'\\)', display:false},
      {left:'\\[', right:'\\]', display:true},
    ],
    throwOnError: false,
  });
}

// ---- DOUBTS: the question one student asks, and the answer the section
// ---- gives it, under the note it is about.
//
// Explain covers a passage; this covers the thing Explain cannot, which is
// "why did she do it that way" at one in the morning. Anybody approved asks
// and anybody approved answers -- deliberately including students, who may not
// upload, may not record and may not spend the class's API budget, and for
// whom this is the first thing in the whole app they can give rather than
// take. Nothing here calls an API either: the answers come from classmates,
// and the one thing that costs money is still Explain, once per passage.
//
// Every body on this screen is somebody's typing and every one of them is put
// on the page with textContent. Not markdown, not innerHTML, not mdSafe: a
// doubt is a sentence and not a document, so there is nothing to gain from
// parsing it and a tag inside one is simply four words that look like a tag.
let THREAD = [];        // the open thread's questions, newest first
// What the thread hangs off: {subject, title} for a lecture note, or
// {material: id} for an uploaded file. It is sent as the query string and as
// the body of every write, so the server is told which thread this is in the
// same words both ways and this page never has to know which kind it is.
let threadOn = null;
let threadBox = null;   // where it is drawn -- #doubts, or a row on a shelf
let threadWord = 'Doubts';
const doubtsBox = document.getElementById('doubts');

// Typing, drawn as typing. The single most important line in this section.
function said(x) {
  const p = document.createElement('p');
  p.className = 'said';
  p.textContent = x.body;
  return p;
}

const saidBy = x => saidByFace(x.by, x.at);

// Taking your own words back, or -- inked as the power it is -- somebody
// else's. Offered to nobody else, because the policy would only refuse them.
function dropBtn(x) {
  if (!x.mine && !atLeast('admin')) return null;
  const b = document.createElement('button');
  b.textContent = 'Delete';
  if (!x.mine) b.className = 'adm';
  b.onclick = () => writeDoubt({id: x.id, delete: true}, b);
  return b;
}

function acts(...buttons) {
  const row = document.createElement('div');
  row.className = 'acts';
  buttons.filter(Boolean).forEach(b => row.appendChild(b));
  return row;
}

// A textarea and one button. Used for the question at the top of the thread
// and for an answer under a question, because they are the same act.
function askBox(placeholder, label, parent) {
  const box = document.createElement('div');
  box.className = 'askbox';
  const ta = document.createElement('textarea');
  ta.maxLength = 2000;
  ta.placeholder = placeholder;
  ta.setAttribute('aria-label', label);
  const err = document.createElement('p');
  err.className = 'err';
  const go = document.createElement('button');
  go.textContent = label;
  go.onclick = () => {
    if (!ta.value.trim()) return void (err.textContent = 'Type something first.');
    writeDoubt({parent: parent || null, body: ta.value}, go, err);
  };
  box.append(ta, err, go);
  return box;
}

// The vote sits beside the answer rather than under it, so the column of
// arrows reads down the thread and the eye finds the top one without reading.
function answerCard(a, best) {
  const el = document.createElement('div');
  el.className = 'ans' + (best ? ' top' : '');
  const what = document.createElement('div');
  what.className = 'what';
  what.append(said(a), saidBy(a));
  const drop = dropBtn(a);
  if (drop) what.appendChild(acts(drop));
  el.append(voteBtn(a, true), what);
  return el;
}

function doubtCard(q) {
  const el = document.createElement('div');
  el.className = 'dbt';
  // One rule, and it is saidBy's: the face leads the byline, here and on an
  // answer and on the wall alike. A second one in a column of its own would
  // be the same person twice on the same row.
  const what = document.createElement('div');
  what.className = 'what';
  what.append(said(q), saidBy(q));
  el.append(what);
  const reply = document.createElement('button');
  reply.textContent = 'Answer this';
  // Second, always, and worded as the lesser thing it is. A classmate who was
  // in the room beats this every time, and the button that asks one of them
  // is the one the thumb lands on first.
  const robot = document.createElement('button');
  robot.textContent = 'Ask AI';
  const row = acts(reply, robot, dropBtn(q));
  what.appendChild(row);
  // The box appears where it was asked for and only there: a form under every
  // question on the screen is six forms nobody asked for.
  reply.onclick = () => {
    reply.disabled = true;
    what.insertBefore(askBox('Answer ' + q.by + '…', 'Post this answer', q.id), row);
  };
  robot.onclick = () => askTheMachine(q, robot, what, row);
  // Only the top answer is marked, and only when the class actually voted for
  // it: a rule down the side of the one answer with no votes says nothing.
  q.answers.forEach((a, k) => what.appendChild(answerCard(a, k === 0 && a.votes > 0)));
  return el;
}

// One line instead of the thread when there is a reason -- still loading, or
// no server behind this copy of the page at all. The heading stays either way,
// so the section does not appear and disappear under the note as it loads.
function drawDoubts(instead) {
  const box = threadBox || doubtsBox;
  box.innerHTML = '';
  // The heading is the only thing that differs between a note's thread and a
  // file's, and it differs because the words are what tell somebody that
  // typing in it is allowed: "Doubts" under a lecture, "Comments" under a
  // scan of last year's paper. Underneath they are the same rows.
  const h = document.createElement('h2');
  h.textContent = threadWord;
  box.appendChild(h);
  if (instead) return box.appendChild(quiet(instead));
  // Which words, read off what the thread is ON rather than off its heading:
  // a subject's thread on Community is headed with the subject's name, and it
  // is still a place to ask a question rather than to comment on a file.
  const onFile = !!(threadOn && threadOn.material);
  box.appendChild(askBox(
    onFile
      ? 'Say something about this file: a page that is wrong, or what it is.'
      : 'Ask the section about this. Somebody who was there will know.',
    onFile ? 'Post this comment' : 'Ask the section', null));
  if (!THREAD.length) {
    return box.appendChild(quiet(
      onFile
        ? 'Nothing said about this one yet.'
        : 'No questions on this one yet. Asking is worth as much as answering, '
          + 'if you are stuck, somebody else is too.'));
  }
  THREAD.forEach(q => box.appendChild(doubtCard(q)));
}

// Nothing here touches THREAD and nothing here redraws. The answer is put on
// the screen and only on the screen: it was never stored, so a redraw -- which
// any answer posted after it will cause -- is what takes it away again, and
// that is the correct behaviour rather than a bug to work around.
async function askTheMachine(q, btn, what, row) {
  btn.disabled = true;
  busy('Thinking…', true);
  const el = document.createElement('div');
  el.className = 'ans ai';
  const body = document.createElement('div');
  body.className = 'what';
  const meta = document.createElement('p');
  meta.className = 'meta';
  body.appendChild(meta);
  el.appendChild(body);
  what.insertBefore(el, row.nextSibling);
  try {
    const r = await fetch('/doubts/ai', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: q.id}),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || 'could not answer that');
    const out = document.createElement('div');
    mdInto(out, d.text);
    body.insertBefore(out, meta);
    // Said under the answer and not over it, because it is what you want to
    // know after reading one: who said this, and is it staying.
    meta.textContent = 'AI, not a classmate. Not saved: check it against the lecture.';
    busyDone('');
  } catch (e) {
    btn.disabled = false;
    el.remove();
    busyDone('Could not answer that: ' + e.message);
  }
}

async function writeDoubt(payload, btn, err) {
  // Armed for the whole request: a second tap on Ask is the same question
  // asked twice, to the whole section, under the same note.
  if (btn) btn.disabled = true;
  busy('Saving…', true);
  try {
    const r = await fetch('/doubts', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign({}, threadOn, payload)),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || 'could not save that');
    busyDone(payload.delete ? 'Deleted' : 'Posted');
    THREAD = d.doubts || [];
    drawDoubts();
  } catch (e) {
    if (btn) btn.disabled = false;
    if (err) { err.textContent = e.message; busyDone(''); }
    else busyDone('Could not save that: ' + e.message);
  }
}

// Fetched when the note opens, not carried on /data: the notice board is
// thirty lines for the whole class, and this would be every question on every
// lecture in twelve subjects, sent to a phone that is going to read one.
//
// The thread is named the way the library names a note -- a subject and a
// title -- and the server turns that into the lecture it belongs to. A note
// with no row behind it, a static export or a file the database never adopted,
// falls back to the subject's own thread rather than to no thread at all.
async function loadDoubts(on, box, word) {
  if (on) {
    threadOn = on;
    threadBox = box || doubtsBox;
    threadWord = word || 'Doubts';
  }
  if (!threadOn) return;
  const asked = threadOn;
  if (on) { THREAD = []; drawDoubts('Looking…'); }
  try {
    const r = await fetch('/doubts?' + new URLSearchParams(asked));
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    if (threadOn !== asked) return;   // they tapped through while this flew
    THREAD = d.doubts || [];
    drawDoubts();
  } catch {
    // A static export has no server to ask, and a form that cannot post is
    // worse than a sentence saying why.
    THREAD = [];
    drawDoubts('This needs the server. Run: notes.py serve');
  }
}

// LEVEL 3: the note itself. Called only by route(), which has already put the
// right URL in the bar, so this never touches history.
function openNote(n, s) {
  current = n;
  currentCode = s.code;
  rcode.textContent = s.code;
  rcode.style.setProperty('--h', hue(s.code));
  // The chip beside it already says MC1101. Back says which subject that is.
  backBtn.textContent = '‹ ' + s.name;   // back goes to the subject, not the top
  backBtn.setAttribute('aria-label', 'Back to ' + s.name);
  // The masthead: what you are reading. The title was in the row you tapped
  // and in the page's <title>, and on this screen it was nowhere at all.
  mast.hidden = false;
  rtitle.textContent = n.title;
  const day = noteDay(n);
  rmeta.textContent = [n.kind === 'revision' ? 'Revision sheet' : 'Lecture',
                       day ? (+day.slice(8)) + ' ' + MONTHS[+day.slice(5, 7) - 1] : null,
                       questionsOf(n).length
                         ? plural(questionsOf(n).length, 'question') + ' to practise' : null,
                      ].filter(Boolean).join(' · ');
  mdInto(body, n.md);
  // A note whose markdown opens with its own '# title' would now say the same
  // thing twice, once in each size. The masthead is the title; this is the
  // copy of it that came out of the transcriber.
  const own = body.firstElementChild;
  if (own && own.tagName === 'H1') own.remove();
  practice.hidden = !questionsOf(n).length;   // no questions, no practice
  // The dock carries exactly one accent: Practice when there is something to
  // practise, Share when there is not. Otherwise it has none at all.
  document.getElementById('share').classList.toggle('primary', practice.hidden);

  // Wide tables scroll inside their own box instead of stretching the page.
  body.querySelectorAll('table').forEach(t => {
    const box = document.createElement('div');
    box.className = 'scroll-x';
    t.replaceWith(box); box.appendChild(t);
  });

  document.body.classList.add('reading');
  window.scrollTo(0, 0);
  paintSaveBtn();
  loadDoubts({subject: s.code, title: n.title}, doubtsBox, 'Doubts');
}

function closeRead() {
  document.body.classList.remove('reading');
  mast.hidden = true;          // nothing open, nothing to head
  // The note's markup does not stay lying in the pane. Nothing is ever seen
  // in there with no note open -- #read is hidden on every layout until one
  // is -- but a tab change that leaves an article's children behind is how
  // the next note gets laid out on top of the last one's.
  body.innerHTML = '';
  // Everything else the last note left goes with it too: its code chip in the
  // header, and its thread at the foot.
  rcode.textContent = '';
  doubtsBox.innerHTML = '';
  threadOn = null;
  practice.hidden = true;
  // The Explain button is positioned in document coordinates and lives above
  // everything, so leaving a note by the back gesture -- which never taps the
  // page, so selectionchange never fires -- used to strand it over the list,
  // eating taps and stretching the scroll. Every exit routes through here.
  hideAsk();
  current = null;
}

// ---- Practice: the questions every note already ends with. ----------------
// Nothing is parsed and nothing is asked of an API here: the Mac pulled the
// question/answer pairs out of the markdown and shipped them inside DATA. A
// note with none is never offered practice, so no screen here can be empty.
const quiz = document.getElementById('quiz'), qmain = document.getElementById('qmain');
const qq = document.getElementById('qq'), qa = document.getElementById('qa');
const qcount = document.getElementById('qcount'), qsrc = document.getElementById('qsrc');
const qtitle = document.getElementById('qtitle'), qfill = document.querySelector('#qbar i');
const qshow = document.getElementById('qshow'), qright = document.getElementById('qright');
const qwrong = document.getElementById('qwrong'), qretry = document.getElementById('qretry');
const qagain = document.getElementById('qagain'), practice = document.getElementById('practice');

const questionsOf = n => (n && n.questions) || [];

// One note or the whole subject: the same flat list either way, so nothing
// below this knows which kind of quiz it is running. Each item remembers the
// lecture it came from, so a subject quiz can say where to go back and read.
function quizItems(s, note) {
  return (note ? [note] : s.notes)
    .flatMap(n => questionsOf(n).map(
      x => ({q: x.q, a: x.a, from: n.title, code: s.code})));
}

// Every question in the library, for the week before an exam when the subject
// you are weakest in is not the one you would have picked.
const allItems = () => DATA.flatMap(s => quizItems(s, null));

// ---- Spaced repetition, on this phone. -----------------------------------
// In localStorage, per device, not in the database, and deliberately: the
// static export has no database to write to, a schedule is worth nothing to
// anybody but the person who earned it, and this way practice costs the
// server nothing at all. Losing it costs you an ordering, never a note.
//
// A wrong answer is due again immediately, so it comes back inside this run's
// next round and at the front of the next run. A right one waits longer each
// time it stays right. Questions are keyed by their own text, so notes can be
// regenerated without resetting what you know.
const SR_KEY = 'recarve.sr';
const SR_DAYS = [0, 1, 3, 7, 21];
const srId = it => it.code + '|' + it.from + '|' + it.q;
const srAll = () => {
  try { return JSON.parse(localStorage.getItem(SR_KEY)) || {}; } catch (e) { return {}; }
};

function srMark(it, ok) {
  const all = srAll(), was = all[srId(it)] || {b: 0, w: 0};
  const box = ok ? Math.min(was.b + 1, SR_DAYS.length - 1) : 0;
  all[srId(it)] = {b: box, d: Date.now() + SR_DAYS[box] * 86400000,
                   w: was.w + (ok ? 0 : 1)};
  try { localStorage.setItem(SR_KEY, JSON.stringify(all)); } catch (e) {}
}

// Due first -- and the ones you have got wrong most often ahead of the rest of
// the due -- then questions never seen, then what is not due yet. Sorting is
// stable, so within a rank the notes stay in the order they were recorded.
function srOrder(items) {
  const all = srAll(), now = Date.now();
  const rank = it => {
    const s = all[srId(it)];
    if (!s) return 0;                          // never seen
    return s.d <= now ? -1 - Math.min(s.w, 9) : s.b;
  };
  return items.map((_, k) => k).sort((x, y) => rank(items[x]) - rank(items[y]));
}

// What is true and countable: how many questions there are, how many you have
// answered at all, how many are due, how many you keep getting wrong. No
// mastery percentage -- self-marked recall cannot measure one.
function srStats(items) {
  const all = srAll(), now = Date.now();
  let seen = 0, due = 0, shaky = 0;
  for (const it of items) {
    const s = all[srId(it)];
    if (!s) continue;
    seen++;
    if (s.d <= now) due++;
    if (s.w && s.b < 2) shaky++;
  }
  return {total: items.length, seen, due, shaky};
}

const srSays = st => plural(st.total, 'question') + ' \u00b7 ' + st.seen + ' seen'
  + (st.due ? ' \u00b7 ' + st.due + ' due' : '')
  + (st.shaky ? ' \u00b7 ' + st.shaky + ' still shaky' : '');

// An exam on the institute's own calendar changes what a practice run is for,
// so the row says so. Nothing is fetched for this -- Home already holds it.
function examSoon() {
  const today = attToday();
  for (const n of (ATT && ATT.upcoming) || []) {
    if (n.kind !== 'exam') continue;
    const days = Math.round(
      (new Date(n.date + 'T00:00:00') - new Date(today + 'T00:00:00')) / 86400000);
    if (days > 21) break;
    return days <= 0 ? n.title + ', on now' : n.title + ' in ' + days + ' days';
  }
  return '';
}

let qz = null;   // {key, items, order, i, marks}; order holds indexes into items

// localStorage throws outright in private mode, so every touch is guarded. The
// cost of a failure is losing your place, never the page.
function qLoad(key, n) {
  try {
    const s = JSON.parse(localStorage.getItem(key));
    // Notes get regenerated. A half-finished run against a different set of
    // questions means nothing, so drop it rather than resume the wrong thing.
    if (s && s.n === n && Array.isArray(s.order) && Array.isArray(s.marks)) return s;
  } catch (e) {}
  return null;
}

function qSave() {
  if (!qz) return;
  try {
    localStorage.setItem(qz.key, JSON.stringify(
      {n: qz.items.length, order: qz.order, i: qz.i, marks: qz.marks}));
  } catch (e) {}
}

// One note, one subject, or the whole library: same run, different list. A
// half-finished run is resumed exactly as it was left, schedule and all --
// re-sorting under somebody mid-run would move the question they are on.
function qRun(key, title, items, oneNote) {
  if (!items.length) return;
  const was = qLoad(key, items.length);
  qz = {key, items, oneNote, order: was ? was.order : srOrder(items),
        i: was ? was.i : 0, marks: was ? was.marks : []};
  qtitle.textContent = title;
  quiz.hidden = false;
  qStep();
}

function qOpen(s, note) {
  qRun('recarve.quiz.' + s.code + (note ? '/' + note.title : ''),
       note ? note.title : 'Practice ' + s.name, quizItems(s, note), !!note);
}

const qOpenAll = () => qRun('recarve.quiz.*', 'Practice everything', allItems(), false);

function qStep() {
  const done = qz.i >= qz.order.length;
  qa.hidden = true;
  qshow.hidden = done; qright.hidden = true; qwrong.hidden = true;
  qretry.hidden = true; qagain.hidden = !done;
  qfill.style.width = Math.round(qz.i / qz.order.length * 100) + '%';
  qmain.scrollTop = 0;
  if (done) return qScore();
  const item = qz.items[qz.order[qz.i]];
  qcount.textContent = (qz.i + 1) + ' of ' + qz.order.length;   // where you are
  qsrc.textContent = qz.oneNote ? '' : item.from;
  mdInto(qq, item.q);
}

function qScore() {
  const right = qz.marks.filter(m => m === 1).length;
  const missed = qz.order.filter((_, k) => qz.marks[k] !== 1);
  qcount.textContent = 'Finished';
  qsrc.textContent = srSays(srStats(qz.items));
  qq.innerHTML = '<p id="qscore"></p><p id="qsub"></p>';
  qq.querySelector('#qscore').textContent = right + ' of ' + qz.order.length + ' right';
  qq.querySelector('#qsub').textContent = missed.length
    ? 'Go again on the ones you missed, or start the whole set over.'
    : 'All of them. Nothing left to redo here.';
  qretry.hidden = !missed.length;
  qretry.textContent = 'Try the ' + missed.length + ' I missed';
}

// A round is a set of items; the retry round is just a shorter one.
function qRound(order) {
  qz.order = order; qz.i = 0; qz.marks = [];
  qSave(); qStep();
}

// The answer is never on screen until it is asked for; marking is what moves
// you on, so you cannot skip past a question without saying how it went.
function qReveal() {
  mdInto(qa, qz.items[qz.order[qz.i]].a);
  qa.hidden = false;
  qshow.hidden = true; qright.hidden = false; qwrong.hidden = false;
}
function qMark(ok) {
  srMark(qz.items[qz.order[qz.i]], ok);
  qz.marks[qz.i] = ok ? 1 : 0; qz.i++; qSave(); qStep();
}
function qRetry() { qRound(qz.order.filter((_, k) => qz.marks[k] !== 1)); }
function qAgain() { qRound(qz.items.map((_, k) => k)); }

qshow.onclick = qReveal;
qright.onclick = () => qMark(true);
qwrong.onclick = () => qMark(false);
qretry.onclick = qRetry;
qagain.onclick = qAgain;
document.getElementById('qexit').onclick = () => { quiz.hidden = true; };
practice.onclick = () => {
  const s = subjectOf(view.code);
  if (s && current) qOpen(s, current);
};

function plain(md) {
  return md.replace(/^#+ /gm, '').replace(/\*\*/g, '')
           .replace(/<\/?details>|<\/?summary>/g, '').replace(/\n{3,}/g, '\n\n');
}

function save(name, text, mime) {
  const url = URL.createObjectURL(new Blob([text], {type: mime}));
  const a = document.createElement('a');
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function flash(btn, word) {
  const was = btn.textContent;
  btn.textContent = word;
  setTimeout(() => { btn.textContent = was; }, 1400);
}

// ---- One progress indicator, shared by everything slow. ------------------
// waiting() drops the same indeterminate bar into any element -- that is how
// the Explain panel gets one. #busy is a strip floating over the header for
// page-level work. Anything slow added later calls busy()/busyDone() and is
// finished; nothing slow should ever just sit there looking frozen.
const busyBox = document.getElementById('busy');
let held = false;   // a deliberate slow call owns the strip; job news waits

function waiting(el, msg) {
  el.innerHTML = '<div class="wait"><i></i></div><p class="waitmsg"></p>';
  el.querySelector('.waitmsg').textContent = msg;
}

// A section that is still fetching, drawn where its rows will be. Every
// screen that waited used to put a line of grey text there instead, which is
// the same shape as a screen with nothing on it -- so "Checking your
// attendance" and "You have marked nothing" read identically for the two
// seconds they are hardest to tell apart.
function waitline(msg) {
  const el = document.createElement('div');
  el.className = 'waitline';
  waiting(el, msg);
  return el;
}

// The same three lines every screen that fetches has to write when it cannot:
// what did not happen, in the shape the empty states already use, and never a
// bare grey sentence flush against the edge of the phone.
function failed(box, what, how) {
  box.className = 'blank bad';
  box.innerHTML = '';
  const a = document.createElement('p'), b = document.createElement('p');
  a.textContent = what;
  b.textContent = how;
  box.append(a, b);
}

function busy(msg, hold) {
  if (held && !hold) return;
  held = held || !!hold;
  waiting(busyBox, msg);
  busyBox.classList.add('on');
}

function busyDone(msg) {
  if (!msg) { held = false; return busyBox.classList.remove('on'); }
  held = true;                    // keep the outcome up long enough to read
  busyBox.innerHTML = '<p class="waitmsg"></p>';
  busyBox.querySelector('.waitmsg').textContent = msg;
  busyBox.classList.add('on');
  setTimeout(() => { held = false; busyBox.classList.remove('on'); }, 3500);
}

backBtn.onclick = () => history.back();
lback.onclick = () => history.back();
tabBtns.forEach(b => { b.onclick = () => go(b.dataset.tab); });
window.onpopstate = route;

// ---- The map, behind the avatar. -----------------------------------------
// Five groups, and they are five questions rather than a filing of screens:
// when am I where, what do I read, who else is here, what is mine, and -- for
// the one person who runs the class -- who is waiting to be let in. The groups
// come from what the screens DO, which is why Home sits with the timetable and
// not with the subject list: Home is today, and so are the three levels under
// Classes that are about the week rather than about a subject.
//
// The four tabs at the bottom stay exactly what they were, the four screens
// you cross between classes. This is the whole map, and the only global way to
// the levels inside them -- your day, the timetable, catching up, what you
// saved, what you have put in -- each of which was reachable from one block on
// one screen and nowhere else.
// ---- The one icon set. Twenty-four box, 1.7 stroke, no fills -- the same
// hand the avatar glyph in the header was already drawn in. Every mark is a
// path string and nothing here is fetched: the map has to be whole on a
// handset with no signal, and an icon font or a sprite file is one more thing
// that can fail to arrive.
const ICON = {
  home:      '<path d="M3.8 10.4 12 4.2l8.2 6.2V20H3.8z"/><path d="M9.6 20v-5.4h4.8V20"/>',
  day:       '<rect x="3.8" y="5.2" width="16.4" height="14.6" rx="2.4"/><path d="M3.8 9.8h16.4M8.4 3.4v3.4M15.6 3.4v3.4"/><circle cx="8.6" cy="14.2" r="1.15" fill="currentColor" stroke="none"/>',
  grid:      '<rect x="3.8" y="4.6" width="16.4" height="15" rx="2.4"/><path d="M3.8 9.4h16.4M9.6 9.4v10.2M15.4 9.4v10.2"/>',
  back:      '<path d="M3.9 9.2V4.8M3.9 9.2h4.4"/><path d="M4.6 9.4A8.2 8.2 0 1 1 4.2 14"/><path d="M12 7.8v4.5l3 1.9"/>',
  book:      '<path d="M12 7.3C10.6 6.1 8.7 5.5 4.8 5.5v12.2c3.9 0 5.8.6 7.2 1.8 1.4-1.2 3.3-1.8 7.2-1.8V5.5c-3.9 0-5.8.6-7.2 1.8z"/><path d="M12 7.3v12.2"/>',
  paper:     '<path d="M6.2 3.8h6.6L18.4 9.4v10.8H6.2z"/><path d="M12.6 3.8v5.8h5.8"/><path d="M9.2 13.4h6M9.2 16.6h4"/>',
  bookmark:  '<path d="M7 4.2h10v15.6l-5-3.7-5 3.7z"/>',
  pin:       '<path d="M12 20.8s6.4-5.9 6.4-10.1a6.4 6.4 0 1 0-12.8 0C5.6 14.9 12 20.8 12 20.8z"/><circle cx="12" cy="10.4" r="2.3"/>',
  people:    '<circle cx="9" cy="8.6" r="3.3"/><path d="M2.9 19.6c.7-3.4 3.1-5.4 6.1-5.4s5.4 2 6.1 5.4"/><path d="M16.1 5.7a3.3 3.3 0 0 1 0 5.8M17 14.6c2.1.6 3.4 2.4 3.8 5"/>',
  person:    '<circle cx="12" cy="8.6" r="3.7"/><path d="M4.9 20c.8-3.7 3.7-5.8 7.1-5.8s6.3 2.1 7.1 5.8"/>',
  rise:      '<path d="M3.8 17.8 9.2 12l3.6 3.4 7-7.8"/><path d="M15.4 7.6h4.4v4.4"/>',
  info:      '<circle cx="12" cy="12" r="8.4"/><path d="M12 11.2v5.2M12 7.9h.01"/>',
  shield:    '<path d="M12 3.6 19 6.2v5.3c0 4.2-2.8 7.2-7 8.7-4.2-1.5-7-4.5-7-8.7V6.2z"/>',
  sun:       '<circle cx="12" cy="12" r="4.1"/><path d="M12 2.9v2.3M12 18.8v2.3M4.9 4.9l1.6 1.6M17.5 17.5l1.6 1.6M2.9 12h2.3M18.8 12h2.3M4.9 19.1l1.6-1.6M17.5 6.5l1.6-1.6"/>',
  moon:      '<path d="M20.4 14.2A8.6 8.6 0 0 1 9.8 3.6 8.6 8.6 0 1 0 20.4 14.2z"/>',
};
// One <svg> from one path string, and nothing else in the app draws one.
const icon = (name) => {
  const el = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  el.setAttribute('viewBox', '0 0 24 24');
  el.setAttribute('fill', 'none');
  el.setAttribute('stroke', 'currentColor');
  el.setAttribute('stroke-width', '1.7');
  el.setAttribute('stroke-linecap', 'round');
  el.setAttribute('stroke-linejoin', 'round');
  el.setAttribute('aria-hidden', 'true');
  el.innerHTML = ICON[name];
  return el;
};

// Five sections, and the four that the thumb bar names are four of them. A
// section with levels inside it opens to show them and is a <details>; a
// section that IS one screen is a link and opens nothing. [label, mark,
// where it goes (null for a section that only opens), what is inside it].
const DRAWER = [
  ['Home',      'home',   ['home']],
  ['Classes',   'book',   null, [
    ['Subjects',                  ['classes']],
    ['Your day',                  ['classes', 'day']],
    ['Catching up',               ['classes', 'attendance']],
    ['Past papers',               ['classes', 'papers']],
  ]],
  ['Campus',    'pin',    null, [
    ['Everything on campus',      ['campus']],
    ["What's on",                 ['campus', 'events']],
    ['Clubs and societies',       ['campus', 'clubs']],
    ['Where things are',          ['campus', 'places']],
  ]],
  ['Community', 'people', null, [
    ['All of Community',          ['community']],
    ['The board',                 ['community', 'board']],
    ['Doubts',                    ['community', 'doubts']],
    ['Who has contributed',       ['community', 'standings']],
  ]],
];
// You is not one of the five. It is whose account this is, which is a fact
// about the reader and not a place in the app -- so it stands at the foot of
// the rail behind your own face, where an account has stood in every app
// anybody here already uses, and the four rows open out of it.
const YOU = [
  ['Your profile',              ['me']],
  ['Saved',                     ['me', 'saved']],
  ['Your contributions',        ['me', 'points']],
  ['About recarve',             ['me', 'about']],
];

// Which row is lit. Asked of `view` and not of the hash, because a hash is
// three levels deep inside a subject and the row it belongs under is the one
// at the top of that tab -- reading a lecture is standing in Subjects.
const drawerHere = () => hashOf(view.tab,
  view.sec || view.me || (view.tab === 'classes'
    ? (view.att ? 'attendance' : view.day ? 'day'
       : view.papers ? 'papers' : null)
    : null));

// Repainted when the role arrives or the screen changes, and not on every
// render: render() runs on every vote and every ticked box, and on the wide
// layout the rail is a live part of the page that somebody may have the
// keyboard inside. Rebuilding it under them would drop the focus on the floor.
let drawnFor = null;
function paintDrawer() {
  const here = drawerHere();
  // myName is in the key because the foot of the rail is your face, and that
  // arrives one fetch after the first paint.
  const key = ROLE + '|' + here + '|' + myName;
  if (key === drawnFor) return;
  drawnFor = key;
  dnav.innerHTML = '';
  // One row, whether it is a section that is also a screen or a level inside
  // one. The label is its own element rather than the anchor's text, because
  // the mark has to sit beside it and not inside the sentence.
  const link = (label, parts, mark, href) => {
    const a = document.createElement('a');
    const h = href || hashOf(...parts);
    a.href = h;
    if (mark) a.appendChild(icon(mark));
    const t = document.createElement('span');
    t.textContent = label;
    a.appendChild(t);
    // A real link, so it can be opened in a tab and read out as one -- but
    // this app routes on pushState and not on the hash changing, so the tap
    // itself has to go through go(). A listener rather than .onclick: these
    // are the only handlers in the app built ten at a time on every step,
    // and .onclick puts ten more writes in front of every screen that reads
    // its own controls back out of them.
    if (parts) a.addEventListener('click', (e) => { e.preventDefault(); go(...parts); });
    if (h === here) a.setAttribute('aria-current', 'page');
    return a;
  };
  // A section with levels inside it. <details> is the platform's own
  // disclosure -- it opens with no JavaScript, it is already keyboard and
  // screen-reader correct, and the open one is the one you are standing in.
  // `mark` is an icon's name, or a node already drawn -- the foot of the rail
  // hands it a face, which is the one glyph in here that is made of a name.
  const section = (label, mark, kids) => {
    const d = document.createElement('details');
    d.className = 'nav-sect';
    const sum = document.createElement('summary');
    sum.appendChild(typeof mark === 'string' ? icon(mark) : mark);
    const t = document.createElement('span');
    t.textContent = label;
    sum.appendChild(t);
    d.appendChild(sum);
    const inner = document.createElement('div');
    inner.className = 'kids';
    let holds = false;
    for (const [klabel, kparts] of kids) {
      const a = link(klabel, kparts);
      if (hashOf(...kparts) === here) holds = true;
      inner.appendChild(a);
    }
    d.appendChild(inner);
    // Open on the section you are in, and on nothing else. Where you are is
    // never behind a twisty you have to guess at.
    if (holds) { d.open = true; d.setAttribute('data-here', ''); }
    return d;
  };
  for (const [label, mark, parts, kids] of DRAWER)
    dnav.appendChild(kids ? section(label, mark, kids) : link(label, parts, mark));
  // The admin panel is a page of its own behind its own gate. The row is
  // BUILT for an admin rather than drawn and hidden from everybody else: a
  // hidden link is still a link in the markup, and this app's rule is that the
  // server is the lock and the page does not advertise what it would refuse.
  if (atLeast('admin')) {
    const a = link('Class admin', null, 'shield', '/admin');
    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.textContent = 'Admin';
    a.appendChild(tag);
    dnav.appendChild(a);
  }
  dyou.innerHTML = '';
  dyou.appendChild(section(myName || 'You', face(myName || ''), YOU));
}

// ---- Appearance. The phone's setting is the default and always was; this is
// for the student whose phone is one way and who wants the app the other.
// Stored under one key, read back by the blocking script in the head so the
// first paint is already right, and applied here so the control answers the
// moment it is pressed rather than on the next load.
const THEMES = ['system', 'light', 'dark'];
// The bar at the top of the phone, which is a colour and not a theme: it has
// to be told the resolved answer, because 'system' is not a colour.
const BAR = {light: '#faf9f4', dark: '#0f1115'};
let theme = 'system';
try { const t = localStorage.getItem('theme'); if (THEMES.includes(t)) theme = t; } catch (e) {}

// What the page is ACTUALLY showing, which on 'system' is the phone's answer
// and not the stored word. The button, the bar colour and the next press are
// all asked of this rather than of `theme`.
const isDark = () => theme === 'dark' || (theme === 'system'
  && matchMedia('(prefers-color-scheme: dark)').matches);

const themeBtn = document.getElementById('themebtn');
function paintTheme() {
  if (theme === 'system') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  const dark = isDark();
  const tc = document.getElementById('tc');
  if (tc) tc.setAttribute('content', dark ? BAR.dark : BAR.light);
  // The glyph is what the press would DO, not what you are looking at: a moon
  // on a light page. That is the way round every switch anybody here has used
  // already, and the label says it in words, because a half-moon on its own
  // is a guess either way.
  themeBtn.innerHTML = '';
  themeBtn.appendChild(icon(dark ? 'sun' : 'moon'));
  themeBtn.setAttribute('aria-label', dark ? 'Switch to light' : 'Switch to dark');
}
// 'system' is still the default and still what an untouched install does --
// it is just no longer a third thing to press. The first press says which one
// you meant, and from then on this is a switch with two ends.
themeBtn.onclick = () => {
  theme = isDark() ? 'light' : 'dark';
  try { localStorage.setItem('theme', theme); } catch (e) {}
  paintTheme();
};
// On 'system' the phone can change under the app -- sunset, or the student
// flipping it in Settings with this still open -- and only the bar's colour
// has to be told; the palette is the media query's own job.
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if (theme === 'system') paintTheme();
});
paintTheme();

// Opening remembers what to hand the focus back to, which is not always the
// avatar: on the wide layout the rail is always open and nothing opened it.
let drawerOpener = null;
const drawered = () => document.body.classList.contains('drawered');

function openDrawer() {
  drawerOpener = document.activeElement;
  document.body.classList.add('drawered');
  avatarEl.setAttribute('aria-expanded', 'true');
  paintDrawer();
  const first = drawerEl.querySelector('a,button,summary');
  if (first) first.focus();
}

// Focus goes back to whatever opened it, and only when it was inside the
// drawer when it shut -- a tap on the page behind must not yank the caret up
// to the header. Nothing happens at all when the drawer is the wide layout's
// rail, which is never 'drawered' and must never be closed.
function closeDrawer() {
  if (!drawered()) return;
  document.body.classList.remove('drawered');
  avatarEl.setAttribute('aria-expanded', 'false');
  if (drawerEl.contains(document.activeElement)) (drawerOpener || avatarEl).focus();
  drawerOpener = null;
}

avatarEl.onclick = () => { if (drawered()) closeDrawer(); else openDrawer(); };
document.getElementById('dclose').onclick = closeDrawer;
document.getElementById('scrim').onclick = closeDrawer;

// Escape leaves it, and Tab wraps inside it rather than stepping out onto a
// page that is still behind an open drawer. The trap is the overlay's alone:
// the rail is part of the page and Tab must walk straight out of it into the
// list beside it.
drawerEl.onkeydown = (e) => {
  if (e.key === 'Escape') { e.preventDefault(); return closeDrawer(); }
  if (e.key !== 'Tab' || !drawered()) return;
  const items = Array.from(drawerEl.querySelectorAll('a,button,summary'));
  if (!items.length) return;
  e.preventDefault();
  const step = e.shiftKey ? -1 : 1;
  const at = items.indexOf(document.activeElement);
  items[(at + step + items.length) % items.length].focus();
};

// The other way out, for a thumb: push it back the way it came. Only closing,
// never opening -- an edge swipe to open would be fighting the OS back gesture
// on one side and this app's own horizontal strips (the week, a card's tags)
// everywhere else.
let swipeFrom = null;
drawerEl.addEventListener('touchstart',
  (e) => { swipeFrom = e.touches[0].clientX; }, {passive: true});
drawerEl.addEventListener('touchend', (e) => {
  if (swipeFrom !== null && swipeFrom - e.changedTouches[0].clientX > 60) closeDrawer();
  swipeFrom = null;
}, {passive: true});

// Your own face, in the header, on every screen. /data carries the role and
// not the name -- it is the payload the whole app is built from and a name is
// nobody's business but this one button's -- so the name comes from /me, the
// route the profile screen already reads, asked for once and kept.
//
// Nothing about the button changes if that never answers: no server, a page
// opened as a plain file, or a 403 mid-visit all leave the drawn glyph exactly
// where it was, which is the state this header has always been able to be in.
function drawMyFace(name) {
  if (!name || name === myName) return;
  myName = name;
  avatarEl.innerHTML = '';
  avatarEl.appendChild(face(name));
  paintDrawer();   // the rail's foot is the same face, under the same name
}
function needMyFace() {
  if (myName) return;
  fetch('/me').then(r => r.ok ? r.json() : Promise.reject())
              .then(d => drawMyFace(d.name)).catch(() => {});
}

document.getElementById('save').onclick = async (e) => {
  if (!current) return;
  await toggleBookmark(currentCode, current.title,
                        !isSaved(currentCode, current.title), e.currentTarget);
};

document.getElementById('share').onclick = async (e) => {
  if (!current) return;
  const text = plain(current.md);
  // On a phone this opens the system sheet, so notes go straight to WhatsApp.
  if (navigator.share) {
    try { await navigator.share({title: current.title, text}); return; }
    catch (err) { if (err.name === 'AbortError') return; }
  }
  try { await navigator.clipboard.writeText(text); flash(e.currentTarget, 'Copied'); }
  catch { flash(e.currentTarget, 'Copy failed'); }
};

document.getElementById('dl').onclick = () => {
  if (current) save(current.title + '.md', current.md, 'text/markdown');
};

document.getElementById('print').onclick = () => {
  body.querySelectorAll('details').forEach(d => d.open = true);
  window.print();
};

// ---- Adding things: record, upload, revise. All work happens on the Mac. ----
const sheet = document.getElementById('sheet'), subj = document.getElementById('subj');
const fileInput = document.getElementById('file'), rec = document.getElementById('rec');
const batchName = document.getElementById('batchName'),
      btitle = document.getElementById('btitle'), blabel = document.getElementById('blabel');
let pendingFiles = null;   // files waiting on a shared title, or null
const clock = document.getElementById('clock'), jobsBox = document.getElementById('jobs');
let recorder = null, chunks = [], ticker = null, started = 0, live = false;

// The server is the source of truth once it is running; a plain exported file
// keeps the data that was baked into it.
async function refresh() {
  let answered = false;
  try {
    const r = await fetch('/data');
    answered = true;
    // Same road as a dead socket: 403 (blocked mid-visit) and 503 (Postgres
    // down) are both answers Home has to be able to say something about, and
    // returning here left it on "Checking your timetable…" with the job poll
    // switched off and nothing to switch it back on.
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    DATA.length = 0; DATA.push(...d.subjects);
    // A vote, or a transcription finishing, moves the board. Dropped rather
    // than refetched: Campus asks for it when Campus is opened, and most
    // refreshes happen on a screen that is not looking at it.
    BOARD = null;
    // Everything Home needs rides on this one request.
    TT = d.timetable || [];
    ATT = d.attendance || null;
    BOOKMARKS = d.bookmarks || [];
    PAPER_COUNTS = d.paper_counts || {};
    PENDING = d.pending || 0;
    ANN = d.announcements || [];
    NOW = d.now || NOW;
    ROLE = d.role || ROLE;
    applyRole();
    // A role means there is somebody signed in to have a face. --no-auth
    // sends none, and nobody is exactly who that server has.
    if (d.role) needMyFace();
    markSeen(d.now);
    if (!subj.options.length) {
      for (const c of d.codes) {
        const o = document.createElement('option');
        o.value = c.code; o.textContent = c.code + ', ' + c.name;
        subj.appendChild(o);
      }
    }
    // Before the render, not after: Home reads `live` to decide whether an
    // empty timetable means "set one up" or "there is no server to save it
    // to", and setting it afterwards made a working server say the latter.
    live = true;
    render();
  } catch {
    live = false;
    // A page opened as a plain file has no server behind it. Home has to be
    // able to say so rather than sit on "checking…" forever.
    // Nothing ever answered, so nothing ever will: that is the static export,
    // which has nobody to be and assumes the role that leaves every button
    // working. A server that did answer -- 503, 403 -- has said no, and the
    // Add button stays away rather than 403ing on the tap.
    if (!answered && ROLE === null) { ROLE = 'trusted'; applyRole(); }
    if (TT === null) { TT = []; render(); }
  }
}

function jobRow(j) {
  const el = document.createElement('div');
  el.className = 'job' + (j.state === 'failed' ? ' failed' : '');
  const busy = j.state === 'queued' || j.state === 'transcribing';
  const pct = (j.detail.match(/^(\d+)%/) || [])[1];
  el.innerHTML = (busy ? '<i class="spin"></i>' : '') +
    '<div class="col"><div class="nm"></div>' +
    (pct ? '<div class="bar"><i></i></div>' : '') +
    '</div><span class="st"></span>';
  el.querySelector('.nm').textContent = j.name.replace(/^[A-Z]{2}\d{4}-/, '');
  el.querySelector('.st').textContent = j.detail || j.state;
  if (pct) el.querySelector('.bar i').style.width = pct + '%';
  return el;
}

const logbtn = document.getElementById('logbtn'), logbox = document.getElementById('logbox');
let logOpen = false;
logbtn.onclick = async () => {
  logOpen = !logOpen;
  logbox.classList.toggle('on', logOpen);
  logbtn.textContent = logOpen ? 'Hide activity log' : 'Show activity log';
  logbtn.setAttribute('aria-expanded', logOpen ? 'true' : 'false');
  if (logOpen) await pullLog();
};
async function pullLog() {
  if (!logOpen) return;
  try {
    const r = await fetch('/log');
    const {lines} = await r.json();
    logbox.textContent = lines.slice(-120).join('\n') || 'Nothing yet.';
    logbox.scrollTop = logbox.scrollHeight;
  } catch {}
}

let lastDone = 0, lastJobs = '';
async function pollJobs() {
  if (!live) return;
  try {
    const r = await fetch('/jobs');
    const {jobs} = await r.json();
    JOBS = jobs;
    // Home draws the same jobs under "Needs you", and only when they have
    // actually moved: a rebuild every two seconds fights the thumb.
    const shape = JSON.stringify(jobs.map(j => [j.id, j.state, j.detail]));
    if (shape !== lastJobs) {
      lastJobs = shape;
      if (view.tab === 'home') render();
    }
    jobsBox.innerHTML = '';
    jobs.slice(0, 4).forEach(j => jobsBox.appendChild(jobRow(j)));
    // The jobs list lives on the subject screens, and + now works while you
    // are reading, so a lecture uploaded from a note would otherwise
    // transcribe out of sight. Mirror it into the shared strip.
    const running = jobs.find(j => j.state === 'queued' || j.state === 'transcribing');
    if (running && document.body.classList.contains('reading')) {
      busy(running.name.replace(/^[A-Z]{2}\d{4}-/, '') + ' · '
           + (running.detail || running.state));
    } else if (!held) {
      busyBox.classList.remove('on');
    }
    const done = jobs.filter(j => j.state === 'done').length;
    if (done !== lastDone) { lastDone = done; refresh(); }
  } catch {}
}

// Nothing here is the lock -- the server is, and /upload, /revise and /explain
// refuse a student whatever this page shows. What the page owes a student is
// the truth in advance: the control stays where it is, says who it is for and
// what would open it, and never spends a request to come back 403.
//
// Hiding it was the old answer and it was the wrong one. A + button that is
// simply absent reads as an app that does not do that, so the student never
// learns the thing exists, never learns what a trusted member is, and never
// asks the one person who could make them one.
const mayAdd = () => atLeast('trusted');
// What the + does depends on where you are standing. On Community it is the
// composer -- which everybody may use, students included, because words on
// the wall are not an upload -- and everywhere else it is the Add sheet.
const fabWrites = () => view.tab === 'community' && !view.compose;
function paintFab() {
  const fab = document.getElementById('fab');
  // Unknown is not a role. Until /data answers there is nothing honest to say
  // about the + button, so it waits rather than appearing and then locking --
  // and a 503 or a 403 leaves ROLE null, which is not permission either.
  // And it is gone wherever the screen already has a primary action of its
  // own: a composer has Post, and reading has the dock (that one is CSS, on
  // body.reading). A + floating on top of those is a second way to add
  // something you are not doing.
  fab.hidden = ROLE === null || !!view.compose;
  fab.className = (fabWrites() || mayAdd()) ? '' : 'locked';
  fab.setAttribute('aria-label', !fabWrites() ? 'Add a lecture or notes'
    : wallOn === 'feed' ? 'Write a post' : 'Write a confession');
}
function applyRole() {
  paintFab();
  document.getElementById('lock').hidden = mayAdd();
  for (const id of ['opt-rec', 'opt-audio', 'opt-doc', 'opt-revise']) {
    const el = document.getElementById(id);
    el.className = mayAdd() ? 'opt' : 'opt locked';
    // aria-disabled rather than disabled: a disabled button is not focusable,
    // so a screen reader would skip the one row that explains itself.
    el.setAttribute('aria-disabled', mayAdd() ? 'false' : 'true');
  }
}

const openSheet = () => {
  // Adding a chemistry recording from the chemistry screen should not need
  // the dropdown at all. '#me' is not a subject, and setting the select to a
  // value it has no option for blanks it -- which uploads with no subject and
  // loses the recording.
  if (subjectOf(view.code)) subj.value = view.code;
  sheet.classList.add('on');
};
const closeSheet = () => {
  sheet.classList.remove('on'); rec.classList.remove('on'); prog.classList.remove('on');
  batchName.classList.remove('on'); pendingFiles = null;
};
document.getElementById('fab').onclick = () => fabWrites()
  ? go('community', wallOn === 'feed' ? 'say' : 'confess')
  : openSheet();

// ---- The + gets out of the way of what is under it. -----------------------
// Reserving --fabclear at the bottom of every list keeps it off the last row.
// It was on all the other rows: 58px of accent fixed over the middle of the
// screen, on a confession's third line, on a club card's buttons, on a
// paragraph of somebody's notes. So it goes on the way down a screen and
// comes back the moment the screen stops moving or turns round -- a control
// answering a gesture, not a thing that hides on you. Two scrollers, because
// the wide layout scrolls the list column rather than the window.
const listEl = document.getElementById('list');
let fabY = 0, fabRest = 0;
function fabScroll(y) {
  document.body.classList.toggle('fabaway', y > fabY + 6 && y > 80);
  fabY = y;
  clearTimeout(fabRest);
  fabRest = setTimeout(() => document.body.classList.remove('fabaway'), 650);
}
addEventListener('scroll', () => fabScroll(window.scrollY), {passive: true});
listEl.addEventListener('scroll', () => fabScroll(listEl.scrollTop), {passive: true});
document.getElementById('opt-close').onclick = closeSheet;
sheet.onclick = e => { if (e.target === sheet) closeSheet(); };

const prog = document.getElementById('prog'), fill = document.getElementById('fill');
const ptxt = document.getElementById('ptxt');
const mb = b => (b / 1048576).toFixed(1) + ' MB';

// XMLHttpRequest, not fetch: fetch cannot report upload progress at all, so a
// big lecture over wifi looks frozen and people give up mid-transfer.
const LIMIT_AUDIO = __AUDIO_MB__ * 1048576, LIMIT_DOC = __DOC_MB__ * 1048576;
// Photos a browser can draw. HEIC is left out on purpose: most browsers
// cannot show it, and a broken-image icon is worse than the plain link.
const IS_IMAGE = /\.(jpe?g|png|gif|webp)$/i;
const IS_AUDIO = /\.(m4a|mp3|wav|mp4|mov|aac|ogg|opus|flac|mkv|webm)$/i;

function upload(blob, name) {
  prog.classList.remove('err');
  prog.classList.add('on');
  fill.style.width = '0%';
  ptxt.textContent = 'Starting…';

  // The server refuses this too, and its refusal is the one that counts. This
  // one saves a phone from spending ten minutes of mobile data on a 413.
  const cap = IS_AUDIO.test(name) ? LIMIT_AUDIO : LIMIT_DOC;
  if (blob.size > cap) {
    prog.classList.add('err');
    fill.style.width = '100%';
    ptxt.textContent = `${name} is ${mb(blob.size)}, the limit is ${mb(cap)}`;
    return;
  }

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/upload');
  xhr.setRequestHeader('X-Filename', encodeURIComponent(name));
  xhr.setRequestHeader('X-Subject', subj.value);
  xhr.setRequestHeader('Content-Type', 'application/octet-stream');
  xhr.timeout = 30 * 60 * 1000;   // an hour-long recording over wifi is slow

  xhr.upload.onprogress = e => {
    if (!e.lengthComputable) return;
    const pct = Math.round(e.loaded / e.total * 100);
    fill.style.width = pct + '%';
    ptxt.textContent = `${pct}%  ·  ${mb(e.loaded)} of ${mb(e.total)}`;
  };

  // Retrying works for a dropped connection; it cannot work for a no.
  const fail = (msg, retry = true) => {
    prog.classList.add('err');
    fill.style.width = '100%';
    ptxt.textContent = msg + (retry ? ', tap an option to try again' : '');
  };

  xhr.onload = () => {
    let d = {};
    try { d = JSON.parse(xhr.responseText); } catch {}
    // The gate's own words are a route name and a role. Say what the Me tab
    // says instead, and do not invite a retry that cannot succeed.
    if (xhr.status === 403)
      return fail(LOCK_ADD, false);
    if (xhr.status !== 200) return fail(d.error || `Upload failed (${xhr.status})`);
    fill.style.width = '100%';
    ptxt.textContent = 'Uploaded. Making notes…';
    pollJobs();
    setTimeout(() => { closeSheet(); prog.classList.remove('on'); }, 900);
  };
  xhr.onerror = () => fail('Lost connection');
  xhr.ontimeout = () => fail('Upload timed out');
  xhr.onabort = () => fail('Upload cancelled');

  xhr.send(blob);
}

// Several files, one after another rather than in parallel: a phone on the
// class wifi with 109 other phones on it is not fighting itself for
// bandwidth, and the progress bar can say something true -- which file, out
// of how many -- instead of an average of five uploads nobody can act on.
//
// A deliberate near-copy of upload() rather than a shared core the two call
// into. upload() is the path every recording and every single-file add has
// gone through for as long as this app has existed, and refactoring it to
// serve a second caller risked the one flow that must never break for the
// sake of the one that is new today.
async function uploadBatch(files, title) {
  const batch = crypto.randomUUID();
  prog.classList.remove('err');
  prog.classList.add('on');
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    // The picker that reaches this only ever offers documents and photos --
    // opt-audio never sets fileInput.multiple -- so LIMIT_DOC is the only cap
    // that applies here.
    if (f.size > LIMIT_DOC) {
      prog.classList.add('err');
      fill.style.width = '100%';
      ptxt.textContent = `${f.name} is ${mb(f.size)}, the limit is ${mb(LIMIT_DOC)}`;
      return;
    }
    const label = `File ${i + 1} of ${files.length}`;
    const ok = await new Promise((resolve) => {
      fill.style.width = '0%';
      ptxt.textContent = `${label}: Starting…`;
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/upload');
      xhr.setRequestHeader('X-Filename', encodeURIComponent(f.name));
      xhr.setRequestHeader('X-Subject', subj.value);
      xhr.setRequestHeader('X-Batch', batch);
      xhr.setRequestHeader('X-Title', encodeURIComponent(title));
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.timeout = 30 * 60 * 1000;
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const pct = Math.round(e.loaded / e.total * 100);
        fill.style.width = pct + '%';
        ptxt.textContent = `${label}: ${pct}%  ·  ${mb(e.loaded)} of ${mb(e.total)}`;
      };
      const fail = (msg) => {
        prog.classList.add('err');
        fill.style.width = '100%';
        ptxt.textContent = `${f.name}: ${msg}`;
        resolve(false);
      };
      xhr.onload = () => {
        let d = {};
        try { d = JSON.parse(xhr.responseText); } catch {}
        if (xhr.status === 403) return fail(LOCK_ADD);
        if (xhr.status !== 200) return fail(d.error || `Upload failed (${xhr.status})`);
        resolve(true);
      };
      xhr.onerror = () => fail('Lost connection');
      xhr.ontimeout = () => fail('Upload timed out');
      xhr.onabort = () => fail('Upload cancelled');
      xhr.send(f);
    });
    // Stop rather than skip: uploading the rest out of order would leave a
    // batch with a hole in it and nothing on screen saying which file that
    // hole is, on a title the class will keep reading as one complete thing.
    if (!ok) return;
  }
  fill.style.width = '100%';
  ptxt.textContent = `Uploaded ${files.length} files. Making notes…`;
  pollJobs();
  setTimeout(() => { closeSheet(); prog.classList.remove('on'); }, 900);
}

// Each option refuses to start rather than opening a file picker, filling a
// progress bar and coming back 403. The reason is already on screen above
// them, in #lock, so there is nothing left for the tap to say.
document.getElementById('opt-audio').onclick = () => {
  if (!mayAdd()) return;
  // A recording is one file with one title -- its own -- so this picker never
  // offers more than one, whatever the last picker left the input set to.
  fileInput.multiple = false;
  fileInput.accept = 'audio/*,video/*'; fileInput.click();
};
document.getElementById('opt-doc').onclick = () => {
  if (!mayAdd()) return;
  // image/* was missing entirely until now: a phone photographing a page of
  // handwritten notes -- the single most obvious way a student adds anything
  // -- had no option that would even open the camera roll for it.
  fileInput.multiple = true;
  fileInput.accept = '.pdf,.txt,.md,image/*'; fileInput.click();
};
fileInput.onchange = () => {
  const files = Array.from(fileInput.files);
  fileInput.value = '';
  if (!files.length) return;
  // A recording goes straight up, as it always has: its name becomes the
  // lecture's, and the audio picker is the only one that leaves multiple off.
  if (!fileInput.multiple) return upload(files[0], files[0].name);
  pendingFiles = files;
  if (files.length === 1) {
    // Renamed the moment it is picked, not after: "IMG_4821" on the shelf is
    // a file nobody will ever find again. Pre-filled, so keeping the camera's
    // name is still one tap.
    blabel.textContent = 'Name this file';
    btitle.value = stemOf(files[0].name).replace(/[_]+/g, ' ').trim();
  } else {
    blabel.textContent = `One title for all ${files.length} files`;
    btitle.value = '';
  }
  batchName.classList.add('on');
  btitle.focus();
  btitle.select();
};
const stemOf = n => (n.lastIndexOf('.') > 0 ? n.slice(0, n.lastIndexOf('.')) : n);
const extOf = n => (n.lastIndexOf('.') > 0 ? n.slice(n.lastIndexOf('.')) : '');
btitle.onkeydown = e => { if (e.key === 'Enter') document.getElementById('bgo').click(); };
document.getElementById('bcancel').onclick = () => {
  batchName.classList.remove('on');
  pendingFiles = null;
};
document.getElementById('bgo').onclick = () => {
  const title = btitle.value.trim();
  if (!title) return btitle.focus();
  const files = pendingFiles;
  pendingFiles = null;
  batchName.classList.remove('on');
  // One file: the typed name IS its filename, with the original extension
  // kept -- the extension is what the server files it by. Several: one shared
  // title, each file keeping its own name inside the group.
  if (files.length === 1) return upload(files[0], title + extOf(files[0].name));
  uploadBatch(files, title);
};

document.getElementById('opt-rec').onclick = async () => {
  if (!mayAdd()) return;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    chunks = [];
    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = e => e.data.size && chunks.push(e.data);
    recorder.onstop = () => {
      stream.getTracks().forEach(t => t.stop());
      // Safari records mp4, Chrome webm; ffmpeg on the Mac reads both.
      const type = recorder.mimeType || 'audio/webm';
      const ext = type.includes('mp4') ? 'mp4' : 'webm';
      const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-');
      upload(new Blob(chunks, {type}), `${subj.value}-${stamp}.${ext}`);
    };
    recorder.start(5000);  // flush every 5s so a crash does not lose everything
    started = Date.now();
    rec.classList.add('on');
    ticker = setInterval(() => {
      const s = Math.floor((Date.now() - started) / 1000);
      clock.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }, 500);
  } catch (e) {
    alert('Microphone blocked. On iPhone this needs https or localhost.');
  }
};

document.getElementById('opt-stop').onclick = () => {
  if (recorder && recorder.state !== 'inactive') recorder.stop();
  clearInterval(ticker);
  rec.classList.remove('on');
};

document.getElementById('opt-revise').onclick = async () => {
  if (!mayAdd()) return;
  const code = subj.value;
  closeSheet();
  if (!code) return busyDone('Pick a subject first');
  // Closed sheet, strip on, request in flight: the rest of the app keeps
  // working for the half minute this takes.
  busy('Building the revision sheet for ' + code + '. This takes 10-40 seconds.', true);
  try {
    const r = await fetch('/revise', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({subject: code}),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || 'could not build a revision sheet');
    await refresh();               // the sheet is a note now; show it
    busyDone('Revision sheet ready for ' + code);
  } catch (e) {
    busyDone('Revision sheet failed: ' + e.message);
  }
};

applyRole();   // hidden until /data says otherwise, not hidden once it says no
// Best-effort and silent either way: a static export has no origin a service
// worker can run on, and an older browser has no navigator.serviceWorker at
// all. Neither is a failure this app has anything to say about.
if (navigator.serviceWorker) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
refresh();
setInterval(pollJobs, 2000);
setInterval(pullLog, 3000);

// ---- Explain: highlight anything in a note, tap the button ----
const ask = document.getElementById('ask'), panel = document.getElementById('panel');
const out = document.getElementById('out'), quote = document.getElementById('quote');
let picked = '';

function hideAsk() { ask.classList.remove('on'); }

document.addEventListener('selectionchange', () => {
  const sel = document.getSelection();
  const text = sel ? sel.toString().trim() : '';
  // Only offer it for a real phrase inside a note, not a stray tap.
  // ROLE null, not !mayAdd(): until the server has said who this is there is
  // nothing to offer and nothing to refuse. A student is offered the button
  // and told, in the panel it opens, who Explain is for.
  if (!text || text.length < 12 || !current || ROLE === null
      || !body.contains(sel.anchorNode) || panel.classList.contains('on')) {
    return hideAsk();
  }
  picked = text;
  const r = sel.getRangeAt(0).getBoundingClientRect();
  // Never inside the sticky header: the strip is 65px tall, so this is the
  // first line under it. Above it the button is only half-visible anyway.
  ask.style.top = Math.max(window.scrollY + 70,
                           window.scrollY + r.top - 52) + 'px';
  ask.style.left = Math.max(12, Math.min(window.innerWidth - 130,
                                         r.left + r.width / 2 - 55)) + 'px';
  if (mayAdd()) ask.classList.remove('locked');
  else ask.classList.add('locked');
  ask.classList.add('on');
});

ask.onclick = async () => {
  hideAsk();
  quote.textContent = picked;
  panel.classList.add('on');
  // The refusal arrives in the panel the button opens, in the same sentence
  // the Me tab and the add sheet use, rather than as a 403 dressed as a
  // failure. No request is made: this one costs money when it succeeds.
  if (!mayAdd()) { out.textContent = LOCK_EXPLAIN; return; }
  waiting(out, 'Reading that passage\u2026');
  try {
    const res = await fetch('/explain', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: picked, title: current ? current.title : ''}),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.status);
    mdInto(out, data.text);
  } catch (err) {
    // The static export has no server, so say that rather than "failed".
    out.textContent = String(err).includes('JSON') || String(err).includes('Failed to fetch')
      ? 'Explain needs the server. Run: notes.py serve'
      : 'Could not explain that: ' + err.message;
  }
};

document.getElementById('close').onclick = () => panel.classList.remove('on');

q.oninput = render;

// A deep link arrives as one history entry, so back would leave the app rather
// than climb a step. Seed every step above it -- the tab, then each level
// inside it -- before the first route, so back walks out the way you would
// have walked in.
function seedHistory() {
  const deep = location.hash.slice(1).split('/').filter(Boolean);
  if (!deep.length) return;
  const here = location.hash;
  history.replaceState(null, '', '#home');
  for (let k = 1; k < deep.length; k++)
    history.pushState(null, '', '#' + deep.slice(0, k).join('/'));
  history.pushState(null, '', here);
}
seedHistory();
route();
</script>
""".replace("__CSS__", (Path(__file__).parent / "web/app.css").read_text())\
     .replace("__AUDIO_MB__", str(MAX_AUDIO_BYTES // 1048576)).replace("__DOC_MB__", str(MAX_DOC_BYTES // 1048576)))


# A note's questions live in <details><summary>Answer</summary> blocks. The
# lecture transcript at the bottom is a <details> too, which is why the summary
# text has to say "answer" before a block counts as one.
ANSWER_BLOCK = re.compile(
    r"<details[^>]*>\s*<summary[^>]*>(?P<head>.*?)</summary>(?P<body>.*?)</details>",
    re.S | re.I,
)
# "**3. State the power rule**", "Q3: ...", "3) ..." -- the number is decoration.
NUMBERED = re.compile(r"^(?:Q(?:uestion)?\s*)?\d+\s*[.):]\s*", re.I)


def _question_above(text):
    """The question sitting directly above an answer block, undecorated."""
    lines, head = [], ""
    for line in reversed(text.rstrip().splitlines()):
        s = line.strip()
        if not s:
            break
        if s.startswith("#"):
            # "### Q2. State the power rule" is itself the question; "## Questions"
            # is only the section it lives in. Numbering is what tells them apart.
            s = s.lstrip("#").strip()
            head = s if NUMBERED.match(s.strip("*_ ")) else ""
            break
        lines.insert(0, s)
    q = (" ".join(lines) or head).strip("*_ ").strip()
    return NUMBERED.sub("", q).strip("*_ ").strip()


def parse_questions(md):
    """The exam questions already in a note, as [{"q": ..., "a": ...}, ...].

    Notes are model-generated, so the shape wanders: the heading is "## Questions"
    in a lecture and "## Likely questions" in a revision sheet, and the question
    itself may be bold, numbered, a sub-heading, or none of those. The one thing
    every version does is put an answer block directly under its question, so
    that pair is what this keys on rather than any heading.

    Deliberately forgiving: anything it cannot read it drops. A note with nothing
    parseable comes back empty and the phone offers no practice for it, which is
    the point -- an empty quiz is worse than no quiz.
    """
    out, pos = [], 0
    for m in ANSWER_BLOCK.finditer(md or ""):
        before, pos = md[pos:m.start()], m.end()
        if "answer" not in m.group("head").lower():
            continue
        q, a = _question_above(before), m.group("body").strip()
        if q and a:
            out.append({"q": q, "a": a})
    return out


def mtime(f):
    """The file's mtime in epoch seconds, or 0 if it went away underneath us.

    Listing a folder and stat-ing what came back are two moments, and an upload
    lands between them: do_upload writes `<name>.part` and renames it into
    place, so a phone refreshing while a classmate uploads can stat a name that
    no longer exists. That costs one row out of one payload -- the next refresh
    has the real file -- and must never cost the whole library, which is what
    letting the error reach the socket does.
    """
    try:
        return int(f.stat().st_mtime)
    except OSError:
        return 0


def build_data(library, relative_to):
    """The whole library as plain data: one entry per subject, empty ones too.

    Shared by `export` (baked into the page) and the server's /data endpoint
    (fetched live), so the browser sees the same shape either way.

    Every subject ships even when it holds nothing. The first screen on the
    phone is the list of subjects, and a student has to see that Chemistry is
    there before they can add a chemistry recording to it.

    `kind` separates the revision sheet from the lectures so the phone can
    group them without matching on the title text. `questions` is the note's own
    exam questions, parsed out here so the phone can quiz from them without
    parsing markdown or costing an API call. `at` is the file's mtime in epoch
    seconds, which is how Home decides what arrived since your last visit --
    disk is what knows, and it knows for the static export too.
    """
    lib = Path(library)
    data = []
    for code, (name, _) in SUBJECTS.items():
        folder = lib / f"{code}-{name}"
        notes, uploads = [], []
        rev = folder / "revision.md"
        if rev.is_file():
            text = rev.read_text()
            notes.append({"title": "Revision sheet", "kind": "revision", "md": text,
                          "at": mtime(rev),
                          "questions": parse_questions(text)})
        for md in sorted((folder / "lectures").glob("*.md")):
            text = md.read_text()
            notes.append({"title": md.stem, "kind": "lecture", "md": text,
                          "at": mtime(md),
                          "questions": parse_questions(text)})
        if (folder / "uploads").is_dir():
            for f in sorted((folder / "uploads").glob("*")):
                uploads.append({"name": f.name, "at": mtime(f),
                                "path": os.path.relpath(f, relative_to)})
        data.append({"code": code, "name": name.replace("-", " "),
                     "notes": notes, "uploads": uploads})
    return data


def export(args):
    """Write the whole library to one self-contained HTML page."""
    import json

    lib = Path(args.library)
    if not lib.exists():
        raise SystemExit(f"no library at {lib} yet, transcribe or add something first")

    out = args.out
    data = build_data(lib, out.parent)

    if not any(s["notes"] or s["uploads"] for s in data):
        # Not fatal any more: the page lists the twelve subjects, and it is
        # where you go to put the first thing into one of them.
        print("library is empty, the page will list the subjects and nothing else",
              file=sys.stderr)

    out.parent.mkdir(parents=True, exist_ok=True)
    # </script> inside note text would close the tag early.
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    out.write_text(PAGE.replace("__DATA__", payload))
    lectures = sum(len(s["notes"]) for s in data)
    files = sum(len(s["uploads"]) for s in data)
    print(f"{lectures} lectures, {files} uploads, {len(data)} subjects -> {out}", file=sys.stderr)


REVISE_PROMPT = """You are given every lecture note from one course this semester, in order.

Produce one revision sheet a student can study the night before the exam.

## Covered
A compact map of what the course actually covered, lecture by lecture, one line each.

## Formulas
Every formula from the whole course in a single table: formula, what it is for, which lecture.
This is the section students photograph before walking into the exam, so miss nothing.

## Threads
Ideas that recur across lectures, and how the later ones build on the earlier ones. This is the
part individual lecture notes cannot show and the reason this sheet exists.

## Likely questions
8-12 questions spanning the whole course, weighted toward what the professor repeated or
explicitly called important. Answers in collapsible blocks:
<details><summary>Answer</summary>

...answer...

</details>

## Gaps
Topics the syllabus implies but no lecture covered, and anything the notes flagged as unclear.

Rules:
- Start at "## Covered". No title heading.
- Maths as LaTeX: $...$ inline, $$...$$ display.
- Use only what is in these notes. Never add material from outside them.
- Where two lectures disagree, say so rather than silently picking one."""


def revise(args):
    """Consolidate every lecture in a subject into one revision sheet."""
    code = resolve_subject(args.subject)
    folder = Path(args.library) / f"{code}-{SUBJECTS[code][0]}"
    notes = sorted((folder / "lectures").glob("*.md")) if (folder / "lectures").is_dir() else []
    if not notes:
        raise SystemExit(f"no lectures under {code} yet")

    # Feed the notes, not the transcripts: they are already condensed, so a whole
    # semester still costs less than one lecture's PDFs would.
    parts = []
    for f in notes:
        text = f.read_text().split("---\n\n<details><summary>Full transcript")[0]
        parts.append(f"# {f.stem}\n\n{text}")
    body = "\n\n".join(parts)

    rate_in, rate_out = price_of(args.notes_model)
    est = len(body) / 4 / 1e6 * rate_in + 16000 / 1e6 * rate_out
    if est > args.max_cost:
        raise SystemExit(f"would cost up to ${est:.2f}, over --max-cost ${args.max_cost:.2f}")

    print(f"{code}: {len(notes)} lectures, {len(body) // 1000}k chars", file=sys.stderr)

    import anthropic

    msg = anthropic.Anthropic().messages.create(
        model=args.notes_model,
        max_tokens=16000,
        system=REVISE_PROMPT,
        messages=[{"role": "user", "content": body}],
    )
    if msg.stop_reason == "refusal":
        raise SystemExit("Claude declined to summarise these notes.")
    text = "".join(b.text for b in msg.content if b.type == "text")

    out = folder / "revision.md"
    out.write_text(f"# Revision, {SUBJECTS[code][0].replace('-', ' ')}\n\n{text}\n")
    cost = msg.usage.input_tokens / 1e6 * rate_in + msg.usage.output_tokens / 1e6 * rate_out
    print(f"  {msg.usage.input_tokens} in / {msg.usage.output_tokens} out (~${cost:.3f})",
          file=sys.stderr)
    log(f"-> {out}", "written", 1)


class Jobs:
    """Uploads waiting to be turned into notes.

    One worker thread, deliberately: transcription is the expensive thing and
    every API call in this system happens here, on this machine. Two students
    uploading at once queue up rather than racing.
    """

    def __init__(self, args):
        import threading

        self.args = args
        self.items = []            # newest first, what /jobs returns
        self.lock = threading.Lock()
        self.pending = []
        self.wake = threading.Event()
        self.seq = 0
        # With --remote-workers the Mac is not necessarily the machine serving
        # this, so lectures go to the database queue and a worker claims them.
        # The list below is then a display of what the worker is doing, and the
        # thread has nothing to run -- but the thread still starts, because a
        # document still finishes here and the two modes must not fork.
        self.remote = bool(getattr(args, "remote", False))
        threading.Thread(target=self._run, daemon=True).start()

    def add(self, path, subject, kind, run=True):
        # A document is already on disk by the time we get here, so it is done.
        # Queuing it would park a 2-second PDF behind an 11-minute lecture.
        # run=False lists a lecture without offering to transcribe it: that is
        # what a row a remote worker already holds needs.
        done = kind == "document" or not run
        with self.lock:
            self.seq += 1
            job = {"id": self.seq, "name": path.name, "subject": subject, "kind": kind,
                   # `key` is the audio's path, which is also lectures.audio_key
                   # -- the one handle a remote worker's reports come back with.
                   "key": str(path),
                   "state": "done" if kind == "document" else "queued",
                   "detail": f"filed under {subject}" if kind == "document" else ""}
            self.items.insert(0, job)
            if not (done or self.remote):
                self.pending.append((job, path))
        if not (done or self.remote):
            self.wake.set()
        return job

    def snapshot(self):
        with self.lock:
            return list(self.items)

    def track(self, path, subject, state, detail=""):
        """Say what is happening to one lecture, whoever is doing it.

        A remote worker's progress lands here so /jobs still shows a live
        percentage on the phone. A restart empties this list while the database
        keeps the queue, so a claim for something not listed adds the row back
        rather than reporting into nothing.
        """
        with self.lock:
            job = next((j for j in self.items if j.get("key") == str(path)), None)
        if job is None:
            job = self.add(path, subject, "audio", run=False)
        self._set(job, state, detail)
        return job

    def _set(self, job, state, detail=""):
        with self.lock:
            job["state"] = state
            job["detail"] = detail

    def _run(self):
        while True:
            self.wake.wait()
            while True:
                with self.lock:
                    if not self.pending:
                        self.wake.clear()
                        break
                    job, path = self.pending.pop(0)
                try:
                    self._process(job, path)
                except Exception as e:
                    self._set(job, "failed", f"{type(e).__name__}: {e}")

    def _process(self, job, path):
        import argparse as _ap

        self._set(job, "transcribing", "starting…")

        def progress(done_sec, total_sec, lang):
            pct = int(done_sec / max(total_sec, 1) * 100)
            elapsed = time.time() - t0
            eta = elapsed / max(done_sec, 1) * (total_sec - done_sec)
            self._set(job, "transcribing",
                      f"{pct}% · {int(done_sec)//60} of {int(total_sec)//60} min · ~{hhmm(eta)} left")

        t0 = time.time()
        # Reuse the CLI path exactly, so the web route and the terminal route
        # can never drift apart.
        opts = _ap.Namespace(
            library=self.args.library, subject=job["subject"], lang=None,
            model="large-v3", notes_lang="english", notes_model=self.args.notes_model,
            no_notes=False, outdir=None, force=False, redo_notes=False,
            max_cost=self.args.max_cost, context=False, on_progress=progress,
        )
        self._set(job, "transcribing", "starting…")
        process(path, opts)
        # The notes exist now, so the lecture row that was queued at upload is
        # done -- which is what makes it count toward its uploader's points.
        # The queue is not a person and has no session to act as; this is the
        # worker role the schema grants claim_lecture() to, keyed on the file
        # it just finished.
        if not getattr(self.args, "no_auth", False):
            try:
                with db() as conn:
                    db_mark_transcribed(conn, path)
            except psycopg.Error as e:
                log(f"could not mark {path.name} done: {e}", "db", 1)
        try:
            path.unlink(missing_ok=True)  # transcript is cached; audio is the bulk
            log(f"removed {path.name} from the inbox", "cleanup", 1)
        except OSError:
            pass
        self._set(job, "done", "notes ready")


# --------------------------------------------------------------------------
# Auth: an invite code gets you in, an admin lets you read.
#
# The Postgres schema in supabase/migrations owns every decision here; this is
# a thin client over profiles/invites and the join_with_invite and
# approve_uploader functions. Every query runs as the caller, using the same
# request.jwt.claims setting PostgREST populates in production, so the RLS
# policies are what actually enforce access. The checks in the handler decide
# which screen you see; they are not the only thing standing between a pending
# joiner and the notes.

import csv
import datetime
import hashlib
import hmac
import html
import http.cookies
import json
import secrets
import threading
import uuid

import psycopg

DB_URL = os.environ.get("RECARVE_DB_URL", "postgresql:///recarve_test")
SESSION_COOKIE = "recarve_session"
# Ten minutes of one round trip to Google and back, scoped to the one path
# that reads it. It is not a session and never becomes one.
OAUTH_COOKIE = "recarve_oauth"
ENV_PATH = Path(__file__).resolve().parent / ".env"

# Requests are gated before they are dispatched, so a route added later is
# protected whether or not whoever adds it remembers auth exists. These are the
# only paths that opt out, and adding to this set is the deliberate act.
PUBLIC_PATHS = {"/join", "/login", "/logout", "/auth/google", "/auth/google/callback"}

# Four roles, in order, and the order is the whole of it. A student reads
# everything the class has; trusted adds the things that write content or spend
# money on the API; cr adds the notice board; admin adds the class itself.
# status is the other axis and is checked separately -- a pending admin is
# still pending.
#
# Nothing anywhere asks "is this role one of these strings". Every question is
# "does this role stand at or above that one", asked through RANK -- at the
# gate below, in the handler's at_least(), and in role_rank() in 0045 on the
# Postgres side. A fifth role is a word in this tuple and a rung in that
# function, not an edit to every call site that ever cared.
SECTION = "Section I"

ROLES = ("student", "trusted", "cr", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}

# What each endpoint costs, in the same place as PUBLIC_PATHS and for the same
# reason: the gate below reads this before dispatch, so a route added later is
# refused to everyone but an admin until somebody names its price here. The
# unnamed ones -- /data, /jobs, /log, /vote, /doubts, the library
# itself -- are reads, personal settings, and the one thing a student can give
# the class back. Asking and answering is deliberately not a privilege.
ROLE_REQUIRED = {
    "/explain": "trusted",     # this is the AI spend
    "/upload": "trusted",
    "/revise": "trusted",      # this is the AI spend too
    "/admin": "admin",
    "/pending": "admin",
    "/approve": "admin",
    "/role": "admin",
    "/block": "admin",
    "/remove": "admin",
    "/remove-lecture": "admin",
    "/rename": "admin",
    "/reset": "admin",
    # Posting to a hundred and ten people at once. The class representative is
    # the person the professors actually tell things to, so this is the rung
    # that exists for it -- and everything else on this list stayed where it
    # was, because a notice board is not the invite code or the pending queue.
    "/announce": "cr",
    # The one road to who wrote a confession. An admin has to be able to deal
    # with the person and not only the row -- and nobody else may ask, which
    # is stated here AND inside confession_author() in Postgres, because a
    # route table is not a security boundary and the function is.
    "/confession-author": "admin",
    # Calling a class off changes everybody's denominator, so it is the same
    # bar as adding to the library. Marking your OWN attendance is not here:
    # it is a private note to yourself and every approved member makes them.
    "/cancelled": "trusted",
    # Campus. The directory and the map are the institute's own facts, so an
    # admin keeps them; an event is a date somebody in the section knows about,
    # which is the same bar as adding to the library. Reading all three is
    # nobody's privilege -- /campus is deliberately not here.
    "/club": "admin",
    "/place": "admin",
    "/event": "trusted",
}

# Passwords are stored as typed. The one thing that goes with that decision is
# on the screens where somebody picks one -- the join form and
# SET_PASSWORD_BODY -- which both say not to reuse a password from elsewhere,
# before the box rather than after a refusal.
#
# A password is chosen at the door, so a joined account is never claimable. The
# null-password state still exists, but only an admin reset can produce it now:
# it logs in with the roll number, opens nothing until it is replaced, and the
# profiles.roll_login column is what says a row is allowed to be in it at all.
MIN_PASSWORD = 8

# Failed logins per roll number and per client, in a sliding window. The two
# numbers are far apart on purpose: five is a fat-fingered password on one
# account, and thirty is a browser that cannot be walking a list of 110 roll
# numbers. Fifteen minutes is long enough to make guessing pointless and short
# enough that a classmate who mistyped theirs is not out for the evening.
LOGIN_TRIES = 5
LOGIN_TRIES_PER_CLIENT = 30
LOGIN_WINDOW = 15 * 60

# What a member who has not set a password may reach, and nothing else is. The
# gate reads this beside PUBLIC_PATHS and for the same reason: a route added
# later starts out shut to somebody mid-change rather than open.
PASSWORD_PATHS = {"/password"}

# ---- The super admin. ----------------------------------------------------
# Platform level, not a role: a super admin is not in `sections` at all, and no
# row anywhere says who they are. Everything about this is deliberately beside
# the student path rather than on top of it -- its own prefix, its own cookie,
# its own key, its own limiter -- so that a bug in a student session cannot
# climb into it. The one thing it does reuse is the crypto, because inventing a
# second way to sign a cookie is how one of them ends up wrong.
SUPER_PREFIX = "/super"
SUPER_COOKIE = "recarve_super"

# The students' two numbers are per roll number and per client, and the tight
# one is the per-account one because there are a hundred and ten accounts. Here
# there is one, so that arrangement inverts: an account-wide lockout on the
# only account is a button any stranger can press to shut the owner out of
# their own install. So the tight number is per client -- five wrong passwords
# from one address -- and the loose one is the backstop across every address at
# once, for the grinder that has a thousand of them. Fifty wrong passwords in
# fifteen minutes from anywhere is not somebody mistyping.
#
# The backstop can still shut the owner out, because every global limit can.
# Fifteen minutes and a line in the log is the price; the alternative is a
# distributed guesser with no wall in front of it at all.
SUPER_TRIES = 5
SUPER_TRIES_TOTAL = 50
SUPER_TOTAL_KEY = "super:everybody"

# How long a /super cookie is good for. The students' runs a year because it is
# how they stay signed in on a phone; this is a desk, a password manager and
# four screens, and a cookie that outlives the sitting is a laptop somebody
# left open. The deadline is inside the signature, so it is the server that
# says when it is over rather than the browser that holds it.
SUPER_SESSION = 12 * 60 * 60


def super_admin(env=os.environ):
    """The super admin's (email, password), or None if this install has none.

    Both or neither. An install that sets one of the two has made a typo, not a
    decision, and the direction to be wrong in is the closed one -- with no
    pair there is no /super at all, not even a login box to guess at.
    """
    email = (env.get("RECARVE_SUPER_EMAIL") or "").strip()
    password = env.get("RECARVE_SUPER_PASSWORD") or ""
    return (email, password) if email and password else None


def super_secret(secret, email):
    """The key /super's cookie is signed with: the session key, put through
    HMAC with the email it belongs to.

    Derived rather than separate so there is still one secret to keep and one
    to rotate, and separate rather than shared so the two cookies cannot be
    swapped: a student's cookie carries a tag made with `secret`, which does not
    verify here, and this cookie's tag does not verify there. Neither direction
    is a check somebody has to remember to write -- it is arithmetic.

    The email is in the key, so changing RECARVE_SUPER_EMAIL logs the old one
    out rather than leaving a cookie that outlives the account it names.
    """
    return hmac.new(secret, b"super:" + email.encode(), hashlib.sha256).digest()


class Limiter:
    """Failed attempts per key, in a sliding window.

    In memory, because there is one server: a restart forgiving the guesses so
    far costs less than a table to write them to and a row to clean up.

    ponytail: per process, so a second server would double every limit. Move
    the counts into Postgres if there is ever more than one.
    """

    def __init__(self, window=LOGIN_WINDOW):
        self.window = window
        self.hits = {}
        self.lock = threading.Lock()

    def _live(self, key, now):
        """Whatever is still inside the window, with the rest forgotten."""
        got = [t for t in self.hits.get(key, ()) if now - t < self.window]
        if got:
            self.hits[key] = got
        else:
            self.hits.pop(key, None)
        return got

    def locked(self, key, limit, now=None):
        now = time.time() if now is None else now
        with self.lock:
            return len(self._live(key, now)) >= limit

    def fail(self, key, now=None):
        now = time.time() if now is None else now
        with self.lock:
            got = self._live(key, now)
            got.append(now)
            self.hits[key] = got

    def clear(self, *keys):
        """Getting it right forgives everything that came before it."""
        with self.lock:
            for key in keys:
                self.hits.pop(key, None)


def session_secret(env_path=ENV_PATH):
    """The cookie-signing key, minted into .env the first time it is wanted.

    Regenerating it is the logout-everyone button: every outstanding cookie
    stops verifying at once.
    """
    got = os.environ.get("RECARVE_SECRET")
    if got:
        return got.encode()
    got = secrets.token_urlsafe(32)
    with env_path.open("a") as fh:
        fh.write(f"\nRECARVE_SECRET={got}\n")
    os.environ["RECARVE_SECRET"] = got
    return got.encode()


# How many times a lecture is handed out before the queue gives up on it.
# claim_lecture() counts the attempts; three is enough to ride out a worker
# that was killed mid-transcription twice and not enough to hand a broken file
# round forever.
MAX_ATTEMPTS = 3

# Every worker route lives under this prefix, and the gate reads the prefix
# rather than a list, so a fifth worker route cannot be born reachable by a
# student because somebody forgot to name it.
WORKER_PREFIX = "/worker/"


def worker_token(env_path=ENV_PATH):
    """The shared secret a remote worker presents, minted into .env like the
    cookie key above.

    Read out of .env when the environment does not already carry it, which
    session_secret() has no need to do and this does: the worker is a second
    process, often started from a shell that never sourced .env, and minting a
    fresh token there would leave the two halves of one pair holding different
    secrets and no obvious reason why.
    """
    got = os.environ.get("RECARVE_WORKER_TOKEN")
    if not got and env_path.exists():
        for line in env_path.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "RECARVE_WORKER_TOKEN" and value.strip():
                got = value.strip()
    if not got:
        got = secrets.token_urlsafe(32)
        with env_path.open("a") as fh:
            fh.write(f"\nRECARVE_WORKER_TOKEN={got}\n")
    os.environ["RECARVE_WORKER_TOKEN"] = got
    return got


def sign_session(profile_id, secret):
    sig = hmac.new(secret, str(profile_id).encode(), hashlib.sha256).hexdigest()
    return f"{profile_id}.{sig}"


def unsign_session(raw, secret):
    """The profile id inside a cookie, or None if we did not sign it.

    The cookie is readable -- it is just an id -- but not writable: without the
    secret you cannot produce the tag for an id you did not receive.
    """
    profile_id, _, sig = (raw or "").partition(".")
    if not profile_id or not sig:
        return None
    want = hmac.new(secret, profile_id.encode(), hashlib.sha256).hexdigest()
    return profile_id if hmac.compare_digest(sig, want) else None


def act_as(conn, user_id, local=False):
    """Run subsequent statements as `user_id`, so RLS applies to them.

    user_id=None drops back to the connection's own role, which owns the tables
    and therefore bypasses RLS -- setup and the first-admin promotion only.
    """
    claims = json.dumps({"sub": str(user_id), "role": "authenticated"}) if user_id else ""
    conn.execute("select set_config('request.jwt.claims', %s, %s)", (claims, local))
    conn.execute("select set_config('role', %s, %s)",
                 ("authenticated" if user_id else "none", local))


def db(user_id=None):
    """A fresh connection, optionally already acting as `user_id`."""
    conn = psycopg.connect(DB_URL, autocommit=True)
    if user_id:
        act_as(conn, user_id)
    return conn


def db_join(conn, code, name, roll_no, phone, password):
    """Put a new person through join_with_invite. (id, status, is_admin) or None.

    None means the code was wrong, expired or used up -- and nothing is left
    behind, not the auth row and not the invite use.

    The password is required here and required in the function, with no default
    on either. An account that exists without one is an account any classmate
    who can read a roll number off a list can claim first, and the only way to
    be sure that state never happens is for there to be no way to ask for it.
    """
    user_id = str(uuid.uuid4())
    result = None
    with conn.transaction() as tx:
        # Serialises the count below, so two people joining in the same instant
        # on day zero cannot both come out as admin.
        conn.execute("select pg_advisory_xact_lock(hashtext('recarve-join'))")
        conn.execute(
            "insert into auth.users (id, instance_id, aud, role, email) values "
            "(%s, '00000000-0000-0000-0000-000000000000', 'authenticated', "
            "'authenticated', %s)",
            (user_id, f"{user_id}@recarve.local"),
        )
        act_as(conn, user_id, local=True)
        ok = conn.execute("select join_with_invite(%s, %s, %s, %s, %s)",
                          (code, name, roll_no, phone, password)).fetchone()[0]
        if not ok:
            raise psycopg.Rollback(tx)
        act_as(conn, None, local=True)
        crown_if_first(conn, user_id)
        row = conn.execute("select status, role from profiles where id = %s",
                           (user_id,)).fetchone()
        result = (user_id, row[0], row[1] == "admin")
    return result


# Google is the second door (0048), and the only thing it has to produce -- to
# a server that has no profile for the person yet -- is a scholar number the
# institute published. Three checks stand between the two: Google says it
# verified the address, the address is in the institute's Workspace domain, and
# what stands before the @ is the eleven digits the registrar assigns. After
# those it is a lookup in roll_list, and the section is the registrar's answer
# rather than anybody's typing.
GOOGLE_DOMAIN = os.environ.get("RECARVE_GOOGLE_DOMAIN", "stu.manit.ac.in")


def google_client():
    """The OAuth client id and secret, or None if this install has no Google.

    Two halves of one pair: an install holding one of them is misconfigured
    rather than half enabled, so the button stays off and /auth/google 404s
    rather than sending somebody to a page that cannot answer them.
    """
    cid = (os.environ.get("RECARVE_GOOGLE_CLIENT_ID") or "").strip()
    secret = (os.environ.get("RECARVE_GOOGLE_SECRET") or "").strip()
    return (cid, secret) if cid and secret else None


def scholar_from_email(email, domain=None):
    """The scholar number inside an institute address, or None.

    26112011201@stu.manit.ac.in is a first year. A personal Gmail is not, and
    neither is a staff address in the same domain: the local part has to be the
    eleven digits the registrar assigns, which is also the key of the table it
    is about to be looked up in. Nothing here decides whether the person is
    ours -- roll_list does that -- this only decides whether there is a
    question to ask.
    """
    local, _, host = (email or "").strip().lower().partition("@")
    if host != (domain or GOOGLE_DOMAIN).strip().lower():
        return None
    return local if len(local) == 11 and local.isdigit() else None


def google_claims(id_token):
    """The payload of Google's id_token, unverified on purpose.

    The signature is what protects a token that arrived by way of the browser.
    This one did not: it came back in the body of a POST we made ourselves to
    accounts.google.com over TLS, authenticated with the client secret. There
    is no untrusted party between the two ends to forge it, and verifying a
    signature would mean fetching and caching Google's keys -- a second thing
    to be stale -- for no attacker it excludes.
    """
    import base64

    payload = (id_token or "").split(".")[1:2]
    if not payload:
        return {}
    raw = payload[0] + "=" * (-len(payload[0]) % 4)
    return json.loads(base64.urlsafe_b64decode(raw))


def crown_if_first(conn, user_id):
    """The first person into an empty install is its admin.

    Nobody can approve anybody otherwise, so whichever door let the first
    person in also elects them. Called from inside both doors' transactions,
    under the same advisory lock, so two people arriving in the same instant on
    day zero cannot both come out as admin.
    """
    if conn.execute("select count(*) from profiles").fetchone()[0] == 1:
        # role 'admin', which carries trusted with it: an untrusted uploader's
        # files are forced pending by the materials trigger, and the admin is
        # the one person nobody else can ever publish.
        conn.execute(
            "update profiles set status = 'approved', role = 'admin' where id = %s",
            (user_id,),
        )


def roll_variants(roll):
    """The ways one seat's roll number may already have been typed.

    The institute writes 26I060. The people already in this database wrote
    I60 -- and 00, and 19 -- because they joined by invite, by hand, a year
    before anybody had the registrar's file. Both name the same seat, and a
    door that compares them as strings gives the admin of this install a
    second account as a student and orphans the first.

    '26I060' -> ('26I060', 'I060', 'I60'). Upper case, because roll_login
    already treats a roll number as case-insensitive.
    """
    roll = (roll or "").strip().upper()
    body = roll[2:] if roll[:2].isdigit() else roll
    out = [roll, body]
    if body[:1].isalpha():
        out.append(body[0] + (body[1:].lstrip("0") or "0"))
    return tuple(dict.fromkeys(v for v in out if v))


def db_google(conn, scholar, email):
    """Put a verified institute address through. (id, status, is_admin) or None.

    None means that scholar number is not on the list the registrar published,
    and nothing is left behind.

    Three cases, cheapest and most certain first:

      1. We have seen this address before. That is a sign-in, not a join, and
         it is the common case forever after the first week.
      2. The roll number is already somebody's profile. That is a member who
         joined by invite before this door existed, and the right thing is to
         hand them the account they already have rather than refuse them for
         owning it -- the institute says this address holds that roll number,
         which is a better claim on the profile than the invite code was.
      3. Nobody here yet. A new auth row, and join_with_google writes the
         profile from the list.

    Case 2 adopts only a profile that has no email yet. A profile that already
    carries a different address is two Google accounts claiming one seat, and
    that is a thing for a person to look at, not for a login to resolve.
    """
    row = conn.execute(
        "select id, status, role from profiles where email = %s", (email,)
    ).fetchone()
    if row:
        return (str(row[0]), row[1], row[2] == "admin")

    listed = conn.execute(
        "select roll_no from roll_list where scholar_no = %s", (scholar,)
    ).fetchone()
    if not listed:
        return None

    with conn.transaction():
        conn.execute("select pg_advisory_xact_lock(hashtext('recarve-join'))")
        # Every spelling of the seat, not just the registrar's. See
        # roll_variants: this install's own admin is I60 where the list says
        # 26I060, and matching on the string alone would hand him a new
        # student account and leave his admin one behind.
        spellings = list(roll_variants(listed[0]))
        row = conn.execute(
            "update profiles set email = %s "
            "where upper(roll_no) = any(%s) and email is null "
            "returning id, status, role", (email, spellings)
        ).fetchone()
        if row:
            return (str(row[0]), row[1], row[2] == "admin")
        if conn.execute("select 1 from profiles where upper(roll_no) = any(%s)",
                        (spellings,)).fetchone():
            # Somebody already holds that seat under another address.
            return None

        user_id = str(uuid.uuid4())
        conn.execute(
            "insert into auth.users (id, instance_id, aud, role, email) values "
            "(%s, '00000000-0000-0000-0000-000000000000', 'authenticated', "
            "'authenticated', %s)",
            (user_id, email),
        )
        act_as(conn, user_id, local=True)
        ok = conn.execute("select join_with_google(%s, %s)",
                          (scholar, email)).fetchone()[0]
        act_as(conn, None, local=True)
        if not ok:
            return None
        crown_if_first(conn, user_id)
        got = conn.execute("select status, role from profiles where id = %s",
                           (user_id,)).fetchone()
        return (user_id, got[0], got[1] == "admin")


def normalise_phone(raw):
    """One stored form for a number people type six different ways.

    +91 98765 43210, 091-98765-43210 and 9876543210 are one person, and a class
    list that holds the same student three ways is not a list anybody can ring.
    Indian mobiles are ten digits starting 6-9; a foreign number or a mistyped
    one is refused here with a sentence a first-year can act on, rather than
    stored as a number that will never connect.

    Returns +91XXXXXXXXXX. Raises ValueError, whose message is what the joiner
    is shown.

    ponytail: a landline whose STD code survives the leading-0 strip -- 0755
    2670000, Bhopal -- is ten digits starting 7 and passes. Nothing in the
    length or the prefix separates it from a mobile; only a live HLR lookup
    would, and a wrong "that is not a number" costs more than a number nobody
    texts. Refuse those at the point somebody actually sends an SMS.
    """
    digits = re.sub(r"\D", "", raw or "")
    # Longest first: "0091..." also starts with "0", and stripping the "0"
    # would leave "0919876543210" looking like nothing at all.
    for prefix in ("0091", "091", "91", "0"):
        if len(digits) > 10 and digits.startswith(prefix):
            digits = digits[len(prefix):]
            break
    if not re.fullmatch(r"[6-9]\d{9}", digits):
        raise ValueError("that does not look like a mobile number - "
                         "10 digits, or +91 and 10 digits")
    return "+91" + digits


def db_inviter(conn, code):
    """The name for "Invited by", or None while nobody can invite anybody yet.

    Whoever minted this code if the code is live and we know who minted it,
    otherwise the admin who has been here longest -- which is the answer today,
    since the bootstrap invite has no author to credit.

    Read on the owning connection: the caller is a stranger with no session at
    all, and invites deliberately has no select policy for anyone.

    ponytail: with two admins the line differs by whether the code is live, so
    it is a weak yes/no about a code somebody already holds. One admin today
    and the fallback answers identically. If the class ever has two, look the
    creator up only after a successful join.
    """
    row = conn.execute(
        "select p.name from invites i join profiles p on p.id = i.created_by "
        "where i.code = %s and i.expires_at > now() and i.uses < i.max_uses",
        (code,),
    ).fetchone() if code else None
    if not row:
        row = conn.execute(
            "select name from profiles where role = 'admin' and status = 'approved' "
            "order by created_at limit 1"
        ).fetchone()
    return row[0] if row else None


# Who each signed-in phone is, remembered for a moment. The gate reads a
# principal on every single request -- every static file, every photo, every
# two-second job poll -- and every read was a brand-new Postgres connection,
# because there is no pool and cannot be one while act_as sets the identity on
# the session. A hundred phones that way is more connects a second than
# Postgres will hold; this way it is one per person per PRINCIPAL_TTL.
PRINCIPAL_TTL = 10
_principals = {}
_principals_lock = threading.Lock()


def forget_principal(profile_id):
    """Drop somebody's remembered principal, so a change to them lands now.

    Called by every function below that writes a profile's status, role or
    password -- there rather than in the handlers, so an admin's tap is instant
    however the change is made, and so the next caller cannot forget.
    """
    with _principals_lock:
        _principals.pop(str(profile_id).lower(), None)


def db_principal(conn, profile_id):
    """Who a session belongs to, or None. Read as themselves, so a deleted or
    never-created profile comes back empty rather than trusted."""
    try:
        row = conn.execute(
            "select name, status, role, password is null from profiles where id = %s",
            (profile_id,),
        ).fetchone()
    except psycopg.errors.InvalidTextRepresentation:
        return None  # signed, but not by a version of us that minted uuids
    if not row:
        return None
    # must_set is the whole forced-change flow: it is re-read here on every
    # request, like status, so an admin's reset lands on somebody's next tap
    # rather than whenever their cookie happens to expire.
    return {"id": str(profile_id), "name": row[0], "status": row[1],
            "role": row[2], "admin": row[2] == "admin", "must_set": row[3]}


def db_login(conn, roll, password):
    """(profile_id, status, must_set) for a correct pair, None for anything else.

    One None for a wrong password, an unknown roll number and a blank box
    alike, because the caller turns all three into the same sentence: a form
    that answers "no such roll number" differently is a way to read the class
    list off a page anyone can load.

    Runs on the owning connection, which is the only one available: the caller
    has no session yet, so there is no auth.uid() for a policy to be about.

    The roll number is a password only for a row that says so. roll_login is
    true for the accounts that predate the join form asking for one and for
    somebody an admin has just reset, and for nobody else: joining writes a
    password in the same statement that creates the row, so a new member has
    never been reachable with information printed on a class list. Matched
    without regard to case, because it is typed on a phone keyboard that
    capitalises, and it is a password on its way to being replaced before
    anything opens.
    """
    roll = (roll or "").strip()
    password = password or ""
    if not roll or not password:
        return None
    row = conn.execute(
        "select id, status, roll_no, password, roll_login from profiles "
        "where upper(roll_no) = upper(%s)",
        (roll,),
    ).fetchone()
    if not row:
        return None
    profile_id, status, roll_no, stored, roll_login = row
    if stored is None:
        ok = roll_login and hmac.compare_digest(password.upper().encode(),
                                                (roll_no or "").upper().encode())
    else:
        ok = hmac.compare_digest(password.encode(), stored.encode())
    if not ok:
        return None
    return str(profile_id), status, stored is None


def check_password(new, roll_no):
    """The two rules, in the one place both screens ask about them.

    Shared by the join form and the forced change, so a rule can only be
    tightened in one place and the joiner cannot be held to a different
    standard than the member -- which is how the roll number got in as a
    password the first time.

    The roll number is refused because it is public: it is on every list in the
    institute, so a password equal to it is a password everybody already has.
    Returns the password as it will be stored. Raises ValueError, whose message
    is what the person is shown.
    """
    new = (new or "").strip()
    if len(new) < MIN_PASSWORD:
        raise ValueError(f"a password needs at least {MIN_PASSWORD} characters")
    if new.upper() == (roll_no or "").strip().upper():
        raise ValueError("that is your roll number - pick something else")
    return new


def db_set_password(conn, user_id, new):
    """A member picks their own password. Nobody else's.

    The "edit own name only" policy is what makes that the truth rather than
    the intention: it confines the update to their own row and pins status and
    role to what they already are, so this statement cannot be more than a
    password however it is written.

    Clearing roll_login is the other half: whatever put this row into the
    roll-number state -- an admin's reset, or having existed before the join
    form asked for a password -- is spent the moment one is chosen, and cannot
    be re-entered except by another admin reset.
    """
    row = conn.execute("select roll_no from profiles where id = %s",
                       (user_id,)).fetchone()
    if not row:
        raise ValueError("no such member")
    new = check_password(new, row[0])
    conn.execute(
        "update profiles set password = %s, roll_login = false where id = %s",
        (new, user_id))
    forget_principal(user_id)   # must_set is what the gate reads; it just changed
    return True


def db_reset_password(conn, profile_id):
    """Put somebody back to their roll number, and back through the forced
    change on their next login.

    This is the only thing that opens the roll-number door, and it opens it for
    one named person an admin has just been asked by. Between the reset and
    their next login their account is claimable by anybody who knows the roll
    number, which is everybody -- so it is a thing an admin does while the
    person is on the phone, not a thing left standing.

    Admin-gated by "admins manage profiles", which is the layer under the
    handler's check: a member's own connection has no policy that reaches
    another row, so this updates nothing at all when it is not an admin asking.
    """
    n = conn.execute(
        "update profiles set password = null, roll_login = true where id = %s",
        (profile_id,)).rowcount
    if not n:
        raise ValueError("no such member")
    forget_principal(profile_id)
    return True


def db_pending(conn):
    """Everyone at the door, with the three things an admin decides on.

    The number is here because it is the only one of the three an admin cannot
    look up elsewhere, and "is this the Rahul from our section" is the whole
    question this queue asks. `asked` is seconds on the server's clock, like
    every other timestamp the phone is given, so "2 days ago" is computed
    against a clock the page already trusts rather than the handset's own.
    """
    return [
        {"id": str(r[0]), "name": r[1], "roll_no": r[2], "phone": r[3],
         "asked": int(r[4].timestamp())}
        for r in conn.execute(
            "select id, name, roll_no, phone, created_at from profiles "
            "where status = 'pending' order by created_at"
        )
    ]


def db_members(conn):
    """Everyone in the class and what they may do, for the admin screen.

    The password rides along because it is stored as typed and this screen is
    the only place that is any use: somebody rings the admin from a corridor,
    and the admin reads it back instead of resetting an account they cannot see
    into. Only /admin ever calls this, and only an admin reaches /admin.
    """
    return [
        {"id": str(r[0]), "name": r[1], "roll_no": r[2], "status": r[3],
         "role": r[4], "phone": r[5], "password": r[6]}
        for r in conn.execute(
            "select id, name, roll_no, status, role, phone, password from profiles "
            "where status <> 'pending' order by name"
        )
    ]


def db_set_role(conn, actor_id, profile_id, role):
    """Move somebody up or down the ladder of roles.

    Admin-gated in the database by the "admins manage profiles" policy, so a
    member's connection changes nobody -- this function only decides the two
    things the policy cannot see: that the role is one on the ladder at all,
    and that an admin is not demoting themselves. The second is not paranoia
    about privilege, it is about the class: the admin is the only account that can
    approve joiners, and one mis-tap would leave nobody who can.
    """
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    if str(profile_id) == str(actor_id):
        raise ValueError("you cannot change your own role")
    n = conn.execute("update profiles set role = %s where id = %s",
                     (role, profile_id)).rowcount
    if not n:
        raise ValueError("no such member")
    forget_principal(profile_id)
    return role


def db_set_status(conn, actor_id, profile_id, blocked):
    """Block somebody, or let them back in. Rejecting a joiner is a block.

    Reversible on purpose: nothing here deletes a person. A rejected joiner
    keeps their row, sees the "No access" screen, and an admin who mis-tapped
    puts them back with the same control. Deleting would take their uploads and
    every vote they cast with them.

    The self check is the same one db_set_role makes, for the same reason, and
    the database makes it again in the profiles_no_self_demotion trigger --
    which is the one that holds when this handler is not the caller.
    """
    if str(profile_id) == str(actor_id):
        raise ValueError("you cannot block yourself")
    status = "blocked" if blocked else "approved"
    n = conn.execute("update profiles set status = %s where id = %s",
                     (status, profile_id)).rowcount
    if not n:
        raise ValueError("no such member")
    forget_principal(profile_id)
    return status


def db_reports(conn):
    """What the class has flagged, newest first, for an admin to look at.

    Read as the caller: "admins read reports" is the only select policy on that
    table, so a member's connection sees an empty list here rather than a
    catalogue of what their classmates complained about.
    """
    return [
        {"id": str(r[0]), "material_id": str(r[1]) if r[1] else None,
         "filename": r[2], "subject": r[3], "reason": r[4], "by": r[5],
         "status": r[6], "at": int(r[7].timestamp())}
        for r in conn.execute(
            "select r.id, m.id, m.filename, m.subject_code, r.reason, p.name, "
            "  m.status, r.created_at "
            "from reports r "
            "left join materials m on m.id = r.material_id "
            "join profiles p on p.id = r.reporter_id "
            "order by r.created_at desc"
        )
    ]


def db_remove_material(conn, material_id):
    """Take a file off the shelves. Admin-gated by the materials update policy.

    'removed', not a delete: the row is what the reports point at, and a
    deleted material takes its report with it by cascade -- so the record of
    the complaint would vanish along with the thing complained about.

    The status flip used to be the whole of this, and it did nothing: the
    library is built straight off disk every time (build_data globs the
    uploads folder), and nothing anywhere checked `status` before listing a
    file. A file marked removed stayed exactly as visible and downloadable as
    it was before an admin pressed the button. file_key is the file's real
    path, saved at upload time, so the disk copy is what actually has to go.
    """
    row = conn.execute(
        "update materials set status = 'removed' where id = %s "
        "returning file_key", (material_id,)).fetchone()
    if not row:
        raise ValueError("no such item")
    try:
        Path(row[0]).unlink(missing_ok=True)
    except OSError as e:
        # The row still says removed either way -- that is the fact the class
        # sees. A file a filesystem permission would not let go of is worth a
        # line in the log, not a 500 handed back to an admin who did the right
        # thing.
        log(f"removed {material_id} from the library but could not delete "
            f"{row[0]}: {e}", "admin", 1)
    return True


def clean_name(raw):
    """A name somebody typed, made safe to be a filename: a path can't ride
    in, and a space may stay -- the whole point of renaming is a name a
    person would actually recognise."""
    return re.sub(r"[^A-Za-z0-9._ -]", "_", Path(str(raw or "")).name).strip(" .")[:120]


def db_rename_material(conn, material_id, new_name):
    """Rename one uploaded file, on disk and in its row, keeping its extension.

    Disk first, then the row, and the disk move undone if the row refuses --
    a row pointing at a file that is no longer there is the one outcome this
    must not leave behind. dedupe_path means a rename can no more overwrite a
    neighbour than an upload can.
    """
    stem = clean_name(new_name)
    if not stem:
        raise ValueError("a name cannot be blank")
    row = conn.execute("select file_key from materials where id = %s and "
                       "status <> 'removed'", (material_id,)).fetchone()
    if not row:
        raise ValueError("no such file")
    old = Path(row[0])
    new = old if (stem + old.suffix) == old.name else \
        dedupe_path(old.with_name(stem + old.suffix))
    if new != old:
        old.rename(new)
    try:
        conn.execute("update materials set filename = %s, file_key = %s where id = %s",
                     (new.name, str(new), material_id))
    except Exception:
        if new != old:
            new.rename(old)
        raise
    return new.name


def db_rename_batch(conn, batch_id, title):
    """Rename a group of uploads: one shared title, every file keeps its own."""
    title = (title or "").strip()[:200]
    if not title:
        raise ValueError("a name cannot be blank")
    n = conn.execute("update materials set title = %s where batch_id = %s",
                     (title, batch_id)).rowcount
    if not n:
        raise ValueError("no such group")
    return title


def db_remove_lecture(conn, code, title):
    """Take a recorded lecture's note off the shelves. The row only -- the
    caller deletes the .md file, because the path is subject_dir's business
    and this function only ever holds a connection, not a library root.

    Revision sheets are not reachable here on purpose: they have no row in
    `lectures` at all (db_backfill only ever adopts lectures/*.md, never the
    subject's own revision.md), and unlike a bad recording, a bad revision
    sheet is not stuck -- Revise regenerates it on the next tap.
    """
    if code not in SUBJECTS:
        raise ValueError("no such subject")
    n = conn.execute(
        "delete from lectures where subject_code = %s and title = %s",
        (code, title)).rowcount
    if not n:
        raise ValueError("no such lecture")
    return True


def year_of_study(scholar_no, today=None):
    """Which year they are in, from the two digits the scholar number opens with.

    26... is an intake that arrived in 2026, and a B.Tech year runs July to
    June, so the academic year that started this month is the one to count
    from -- in September 2026 a 26 is in first year and in January 2027 they
    still are. Returns None rather than a guess for anything that is not two
    leading digits, and never returns less than 1: a number from an intake
    that has not arrived yet is a bad number, not a zeroth year.
    """
    today = today or datetime.date.today()
    if not (scholar_no or "")[:2].isdigit():
        return None
    began = 2000 + int(scholar_no[:2])
    academic = today.year if today.month >= 7 else today.year - 1
    return max(1, academic - began + 1)


def db_profile(conn, user_id):
    """The identity half of the Me tab. Read as themselves.

    The branch and the year come off the registrar's list rather than off the
    profile, because they are the registrar's facts and not the student's to
    edit. Left join and matched folded, so somebody the list has never heard
    of -- a senior, a transfer, anybody who joined by invite with a roll number
    that is not on it -- still gets a profile, just without those two lines.
    """
    row = conn.execute(
        "select name, roll_no, phone, role, status from profiles where id = %s",
        (user_id,),
    ).fetchone()
    if not row:
        return {}
    # Their own line of the registrar's list, on the seat rather than the
    # string -- the same same_seat() the policy on roll_list uses, so the row
    # this asks for and the row that policy allows can never disagree.
    listed = conn.execute(
        "select branch, scholar_no from roll_list"
        " where same_seat(roll_no) = same_seat(%s)",
        (row[1],),
    ).fetchone()
    branch, scholar = listed if listed else (None, None)
    return {"name": row[0], "roll_no": row[1], "phone": row[2],
            "role": row[3], "status": row[4], "branch": branch,
            "scholar_no": scholar, "year": year_of_study(scholar)}


def db_edit_profile(conn, user_id, name, phone):
    """A member fixes their own name and number. Nothing else is theirs to fix.

    This writes exactly two columns, and the "edit own name only" policy is what
    makes that the truth rather than the intention: role and status are pinned
    to their current values by its with-check, so a hand-written update that
    reaches for either is refused by the database and not by this function.

    Both are validated here because both are shown to other people: an empty
    name leaves a blank row on the admin screen, and a number nobody can ring
    is the same as no number at all.
    """
    name = (name or "").strip()[:80]
    if not name:
        raise ValueError("a name cannot be blank")
    phone = normalise_phone(phone) if (phone or "").strip() else None
    conn.execute("update profiles set name = %s, phone = %s where id = %s",
                 (name, phone, user_id))
    return {"name": name, "phone": phone}


def db_invite(conn, section=None):
    """The live code an admin passes on, or None if there is not one.

    Read as whoever is asking: invites has no select policy for members, so a
    normal session sees an empty table here rather than a code it could hand
    to the whole college. Never mints one -- issuing invites is a decision,
    not something a screen does on its own while being looked at.

    `section` is for the one caller that is not inside a section: /super reads
    this on the owning connection, where no policy narrows it, so the clause
    the policy would have added has to be said out loud or the super admin
    would be shown some other section's code. An admin's own call passes None
    and is scoped by "admins manage their own section's invites" exactly as it
    always was.
    """
    row = conn.execute(
        "select code from invites where expires_at > now() and uses < max_uses "
        "and (%s::uuid is null or section_id = %s::uuid) "
        "order by expires_at desc limit 1",
        (section, section),
    ).fetchone()
    return row[0] if row else None


def db_approve(conn, profile_id):
    """Let someone in, and publish whatever they uploaded while waiting.

    Both statements are admin-gated inside the database -- the update by the
    "admins manage profiles" policy, approve_uploader by its own is_admin()
    check -- so this fails for a non-admin connection even if the handler let
    it through.
    """
    conn.execute("update profiles set status = 'approved' where id = %s", (profile_id,))
    forget_principal(profile_id)
    return conn.execute("select approve_uploader(%s)", (profile_id,)).fetchone()[0]


def db_record_upload(conn, user_id, code, filename, dest, is_audio,
                      batch=None, title=None):
    """Remember who sent a file, so the library can say so later.

    batch and title are a materials-only idea: several pages of one handout,
    photographed as several files, sharing one opaque id and one name so the
    phone can draw them as the single thing they actually are. A recording is
    already one file with one title -- its own -- so audio never carries
    either.
    """
    if is_audio:
        # dest.stem, not filename: process() writes its notes to
        # lectures/<dest.stem>.md, and that name is the only handle the
        # library has on this row afterwards.
        conn.execute(
            "insert into lectures (subject_code, uploader_id, title, audio_key) "
            "values (%s, %s, %s, %s)",
            (code, user_id, dest.stem, str(dest)),
        )
    else:
        conn.execute(
            "insert into materials (subject_code, uploader_id, filename, file_key, "
            "size_bytes, batch_id, title) values (%s, %s, %s, %s, %s, %s, %s)",
            (code, user_id, filename, str(dest), dest.stat().st_size, batch, title),
        )


def db_mark_transcribed(conn, path):
    """The audio has notes now, so the lecture row queued at upload is done --
    which is what makes it count toward its uploader's points."""
    conn.execute("update lectures set status = 'done' where audio_key = %s", (str(path),))


def db_meta(conn, user_id):
    """Who added each file, and how the class voted on it.

    Keyed the way build_data names things -- (subject, filename) for an upload,
    (subject, note title) for a lecture -- because the library on disk is still
    the truth about what exists. The database only says who, and how popular.

    Read as the caller, so a pending upload nobody may see yet comes back with
    no attribution rather than leaking one.
    """
    mats, lecs = {}, {}
    for mid, code, filename, who, votes, mine, batch, title in conn.execute(
        "select m.id, m.subject_code, m.filename, p.name, "
        "  (select count(*) from votes v where v.material_id = m.id), "
        "  exists (select 1 from votes v "
        "           where v.material_id = m.id and v.voter_id = %s), "
        "  m.batch_id, m.title "
        "from materials m join profiles p on p.id = m.uploader_id",
        (user_id,),
    ):
        mats[(code, filename)] = {"id": str(mid), "by": who,
                                  "votes": votes, "voted": mine,
                                  "batch": str(batch) if batch else None,
                                  "title": title}
    for code, title, who in conn.execute(
        "select l.subject_code, l.title, p.name from lectures l "
        "join profiles p on p.id = l.uploader_id"
    ):
        lecs[(code, title)] = who
    return mats, lecs


def apply_meta(subjects, mats, lecs):
    """Fold attribution and votes into the library, and rank the uploads.

    Lectures keep their date order: they are the class's record of what
    happened, and ranking Tuesday against Wednesday is nonsense. Uploaded notes
    are ranked, so the set everyone found useful sits at the top of the subject.
    """
    for s in subjects:
        for n in s["notes"]:
            n["by"] = lecs.get((s["code"], n["title"]))
        for u in s["uploads"]:
            u.update(mats.get((s["code"], u["name"]), {}))
        s["uploads"].sort(key=lambda u: (-u.get("votes", 0), u["name"]))
    return subjects


def db_vote(conn, item_id, user_id, on, col="material_id"):
    """Add or drop one person's vote, and return the item's new state.

    Nothing here checks whether they have voted already, or whether the thing
    they are voting for is their own: the unique indexes on votes do the first
    and the "vote as yourself" policy does the second, and both raise. That is
    the point -- one place decides, and it is the same place in production as
    in the tests.

    `col` says which kind of thing is being voted for -- an upload, an answer
    to somebody's doubt, or something said on the wall. It is one of exactly
    three identifiers, all written down right here and none of them ever taken
    off a request: the handler maps its own shape of payload onto one of these
    three words, so there is nothing a caller can put in the middle of that
    f-string.
    """
    if col not in ("material_id", "doubt_id", "post_id"):
        raise ValueError("nothing votable is called that")
    if on:
        conn.execute(f"insert into votes ({col}, voter_id) values (%s, %s)",
                     (item_id, user_id))
    else:
        conn.execute(f"delete from votes where {col} = %s and voter_id = %s",
                     (item_id, user_id))
    row = conn.execute(
        f"select count(*), bool_or(voter_id = %s) from votes where {col} = %s",
        (user_id, item_id),
    ).fetchone()
    return {"votes": row[0], "voted": bool(row[1])}


# ---- Doubts: what one student asks, and what the section answers. --------
#
# The cap the database also holds (0025). Trimmed here rather than left to the
# check constraint, because "value too long for type" is not a sentence
# anybody on a phone can act on.
DOUBT_BODY = 2000


def db_lecture_id(conn, code, title):
    """The lecture row behind a note, or None when there is not one.

    The library on disk is still the truth about what exists, and the page
    holds a note by (subject, title) -- see db_meta, which keys the same way.
    So the phone asks by the two things it has and the id is looked up here,
    rather than every note on every screen having to carry one.

    None is not an error. A static export, or a file the database has never
    adopted, has no row -- and the question asked on it belongs to the subject,
    which always exists. That fallback is the whole reason doubts hang off
    subject_code as well as off a lecture.
    """
    row = conn.execute(
        "select id from lectures where subject_code = %s and title = %s "
        "order by recorded_at limit 1", (code, title),
    ).fetchone() if title else None
    return str(row[0]) if row else None


def db_doubts(conn, user_id, code, lecture_id=None, material_id=None):
    """One thread: questions newest first, answers under each, best first.

    One query for both depths, grouped here. Two would be two round trips and
    a window in which an answer arrives between them and lands under nothing.

    Read as the caller, so the select policy decides what is in it -- and a
    hidden question takes its answers with it, because the left join finds no
    parent for them and they match no thread at all.

    Answers are sorted by the votes the class gave them and then oldest first:
    that is the whole point of putting them on the same votes table as the
    notes. A tie goes to whoever answered first, which is the only tiebreak
    that cannot be gamed by answering later.
    """
    questions, answers = [], {}
    for did, parent, body, who, at, mine, votes, voted in conn.execute(
        "select d.id, d.parent_id, d.body, p.name, "
        "  extract(epoch from d.created_at)::bigint, d.author_id = %(me)s, "
        "  (select count(*) from votes v where v.doubt_id = d.id), "
        "  exists (select 1 from votes v "
        "           where v.doubt_id = d.id and v.voter_id = %(me)s) "
        "from doubts d join profiles p on p.id = d.author_id "
        "left join doubts q on q.id = d.parent_id "
        # An answer is on the thread its question is on. coalesce is what says
        # so, and it is why an answer carries neither column of its own.
        "where coalesce(q.subject_code, d.subject_code) = %(code)s "
        "  and coalesce(q.lecture_id, d.lecture_id) is not distinct from %(lec)s "
        # And the third thread key (0036): a question on an uploaded file. A
        # note's thread and a file's thread are both "null" in the other
        # column, so both halves have to be asked or the two would be one.
        "  and coalesce(q.material_id, d.material_id) is not distinct from %(mat)s "
        # Live only. The select policy leaves an author their own hidden row --
        # it has to, or hiding one would be refused for having hidden it -- so
        # what the class reads is asked for here. q.deleted_at is null is true
        # for a question, which has no parent to have been hidden.
        "  and d.deleted_at is null and q.deleted_at is null "
        "order by d.created_at",
        {"me": user_id, "code": code, "lec": lecture_id, "mat": material_id},
    ):
        row = {"id": str(did), "body": body, "by": who, "at": at,
               "mine": mine, "votes": votes, "voted": voted}
        (questions if parent is None else
         answers.setdefault(str(parent), [])).append(row)
    for q in questions:
        q["answers"] = sorted(answers.get(q["id"], []),
                              key=lambda a: (-a["votes"], a["at"]))
    questions.reverse()   # newest question first; the query gave them oldest
    return questions


def db_ask(conn, user_id, code, lecture_id, parent_id, body, material_id=None):
    """Ask the section something, or answer somebody who did.

    Which of the two it is, is parent_id and nothing else -- and whether that
    parent is a question still standing is the insert policy's decision, not
    this function's, so curl holding a stolen cookie cannot build a thread
    three deep any more than the app can.

    An answer is written with no subject and no lecture: it belongs to its
    question, and the question is the one row that says which thread this is.
    """
    body = (body or "").strip()[:DOUBT_BODY]
    if not body:
        raise ValueError("a question needs something in it")
    if parent_id:
        code, lecture_id, material_id = None, None, None
    return str(conn.execute(
        "insert into doubts (subject_code, lecture_id, material_id, parent_id, "
        "author_id, body) values (%s, %s, %s, %s, %s, %s) returning id",
        (code, lecture_id, material_id, parent_id, user_id, body),
    ).fetchone()[0])


def db_bookmarks(conn, user_id):
    """Every note this student has saved, as (subject, title) pairs.

    Keyed the same way the page already holds a note -- see db_lecture_id --
    so the phone can ask "is this one saved" with the two things it already
    has, rather than a lecture id that a revision sheet does not carry.
    """
    return [{"code": c, "title": t} for c, t in conn.execute(
        "select subject_code, title from bookmarks where profile_id = %s "
        "order by created_at desc", (user_id,))]


def db_set_bookmark(conn, user_id, code, title, on):
    """Save a note, or take it back. On or off, never a row that changes."""
    title = (title or "").strip()[:200]
    if code not in SUBJECTS or not title:
        raise ValueError("which note?")
    if on:
        conn.execute(
            "insert into bookmarks (profile_id, subject_code, title) "
            "values (%s, %s, %s) on conflict do nothing", (user_id, code, title))
    else:
        conn.execute(
            "delete from bookmarks where profile_id = %s and subject_code = %s "
            "and title = %s", (user_id, code, title))


def db_hide_doubt(conn, doubt_id):
    """Yours, or anybody's if you are an admin. The row itself stays.

    Nothing here asks whose it is: "your own doubts, or any as admin" is the
    referee, and an update that matches nothing is the refusal -- for a request
    from this app and for one from curl holding a stolen cookie alike.

    rowcount rather than `returning id`: a RETURNING clause makes Postgres
    apply the select policy to the row as it will be, and the row as it will be
    is hidden -- so the statement that hides one would be refused for having
    hidden it.
    """
    if not conn.execute(
        "update doubts set deleted_at = now() where id = %s and deleted_at is null",
        (doubt_id,),
    ).rowcount:
        raise ValueError("no such question, or it is not yours to delete")


# ---- The wall: what the section says to itself, named and unnamed. ------
#
# One table behind both (0035). The difference between them is one column and
# it is the database's to keep, not this file's: byline_id is null for every
# confession, and author_id -- which is not null, ever -- is a column the
# `authenticated` role has no privilege to select. So the anonymity of this
# feature does not rest on any query below being written correctly. A query
# here that asked for author_id would not leak a name; it would raise
# "permission denied for column author_id" and fail loudly on the first
# request, which is the only kind of mistake worth designing for.
POST_BODY = 2000
POST_LIMIT = 60

# Short, because a room is not an essay, and the same number is in the check
# constraint. Trimmed here so a long paste is shortened rather than refused
# with "value too long for type character varying".
CHAT_BODY = 500
CHAT_PAGE = 50


def db_posts(conn, user_id, kind, relative_to=None, limit=POST_LIMIT):
    """One wall, newest first, with its photos and its votes.

    The join is on byline_id and never on author_id. For the feed those are
    the same uuid and the row says who wrote it; for a confession byline_id is
    null, the left join finds nobody, and `by` comes back null -- not a blank
    name, not an id, not a hash of one. Nothing in this payload orders by
    anything but time, either: a stable per-author ordering is a fingerprint.
    """
    rows = [
        {"id": str(pid), "body": body, "by": who, "at": at, "mine": bool(mine),
         "votes": votes, "voted": voted, "photos": [],
         "batch": str(batch) if batch else None}
        for pid, body, who, at, mine, votes, voted, batch in conn.execute(
            "select po.id, po.body, p.name, "
            "  extract(epoch from po.created_at)::bigint, "
            "  po.byline_id = %(me)s, "
            "  (select count(*) from votes v where v.post_id = po.id), "
            "  exists (select 1 from votes v "
            "           where v.post_id = po.id and v.voter_id = %(me)s), "
            "  po.batch_id "
            "from posts po left join profiles p on p.id = po.byline_id "
            # Live only. The select policy leaves an author their own hidden
            # row -- it has to, or hiding one would be refused for having
            # hidden it -- so what the wall shows is asked for here.
            "where po.kind = %(kind)s and po.deleted_at is null "
            "order by po.created_at desc limit %(lim)s",
            {"me": user_id, "kind": kind, "lim": limit},
        )
    ]
    batches = [r["batch"] for r in rows if r["batch"]]
    if batches:
        # The photos are materials rows, written by the upload path that
        # already existed. This is the only place they are read back as a
        # group, and it is one query for the whole page rather than one per
        # post.
        by_batch = {}
        for batch, name, key in conn.execute(
            "select batch_id, filename, file_key from materials "
            "where batch_id = any(%s::uuid[]) and status = 'visible' "
            "order by created_at", (batches,),
        ):
            by_batch.setdefault(str(batch), []).append(
                {"name": name,
                 "path": os.path.relpath(key, relative_to) if relative_to else key})
        for r in rows:
            r["photos"] = by_batch.get(r["batch"], [])
    return rows


def db_post(conn, user_id, kind, body, batch=None):
    """Say something to the section, with your name on it or without.

    Whether the class is told who wrote this is `kind` and nothing else, and
    what that does is the database's: it decides byline_id, it refuses a photo
    on a confession, and it counts the day's confessions in a trigger. None of
    those three is repeated here, because a rule written twice is a rule that
    can be true in one place and false in the other.
    """
    body = (body or "").strip()[:POST_BODY]
    if not body:
        raise ValueError("a post needs something in it")
    if kind not in ("feed", "confession"):
        raise ValueError("which wall?")
    if kind == "confession":
        batch = None
    return str(conn.execute(
        "insert into posts (kind, author_id, body, batch_id) "
        "values (%s, %s, %s, %s) returning id",
        (kind, user_id, body, batch or None),
    ).fetchone()[0])


def db_hide_post(conn, post_id):
    """Yours, or anybody's if you are an admin. The row itself stays.

    Nothing here asks whose it is -- the update policy is the referee, and an
    update that matches nothing is the refusal. That is what makes an admin's
    one tap work on a confession without this function ever being told, or
    ever being able to ask, who wrote it.
    """
    if not conn.execute(
        "update posts set deleted_at = now() where id = %s and deleted_at is null",
        (post_id,),
    ).rowcount:
        raise ValueError("no such post, or it is not yours to take down")


def db_confession_author(conn, post_id):
    """Who wrote one confession. Admins only, and the database decides that.

    The is_admin() check is inside confession_author() in 0035, not in front
    of it: a member who reaches this gets null rather than a name, whatever
    this handler believed about them.
    """
    row = conn.execute("select confession_author(%s)", (post_id,)).fetchone()
    if not row or not row[0]:
        raise ValueError("no such confession")
    return row[0]


# ---- The subject room. Short messages, in order, newest at the bottom. ---


def db_chat(conn, user_id, code, since=None, limit=CHAT_PAGE):
    """A room's messages: the tail on the way in, then only what is new.

    `since` is the whole polling story. The phone holds the last id it has and
    asks for what came after it, so an open room costs a query over an index
    and a reply that is usually empty -- never the history again. Without an
    id the room is opened for the first time and gets its last `limit`
    messages, oldest first, which is the order they are read in.
    """
    if since:
        rows = conn.execute(
            "select m.id, m.body, p.name, "
            "  extract(epoch from m.created_at)::bigint, m.author_id = %s "
            "from messages m join profiles p on p.id = m.author_id "
            "where m.subject_code = %s and m.id > %s and m.deleted_at is null "
            "order by m.id limit %s", (user_id, code, since, limit)).fetchall()
    else:
        # Newest `limit` by id, then turned around: "last fifty" cannot be
        # asked for in ascending order without reading the whole room.
        rows = conn.execute(
            "select * from (select m.id, m.body, p.name, "
            "  extract(epoch from m.created_at)::bigint, m.author_id = %s "
            "from messages m join profiles p on p.id = m.author_id "
            "where m.subject_code = %s and m.deleted_at is null "
            "order by m.id desc limit %s) t order by 1", (user_id, code, limit)).fetchall()
    return [{"id": mid, "body": body, "by": who, "at": at, "mine": mine}
            for mid, body, who, at, mine in rows]


def db_say(conn, user_id, code, body):
    """One message into one room. Who it is from is the session, never the
    request -- the insert policy pins author_id to the caller and refuses
    anything else, so a stolen cookie can still only talk as itself."""
    body = (body or "").strip()[:CHAT_BODY]
    if not body:
        raise ValueError("type something first")
    if code not in SUBJECTS:
        raise ValueError("which subject?")
    return conn.execute(
        "insert into messages (subject_code, author_id, body) "
        "values (%s, %s, %s) returning id", (code, user_id, body),
    ).fetchone()[0]


def db_hide_message(conn, message_id):
    """Take back what you said, or -- as an admin -- what anybody said.

    Hidden, never deleted, for the reason every other table here gives: a room
    is read by a hundred people and a row that is gone is a row nobody can be
    shown again when somebody asks what was said.
    """
    if not conn.execute(
        "update messages set deleted_at = now() where id = %s and deleted_at is null",
        (message_id,),
    ).rowcount:
        raise ValueError("no such message, or it is not yours to remove")


def db_contributions(conn, user_id):
    """What one member has put in, and the points view's read of it.

    Points are status, never a key. Nothing in this app asks what your score is
    before it shows you something; reading a note has no price.
    """
    row = conn.execute(
        "select uploads, recordings, votes_received, score from points where id = %s",
        (user_id,),
    ).fetchone() or (0, 0, 0, 0)
    uploads = [
        {"name": name, "subject": code, "votes": votes, "status": status}
        for name, code, votes, status in conn.execute(
            "select m.filename, m.subject_code, "
            "  (select count(*) from votes v where v.material_id = m.id), m.status "
            "from materials m where m.uploader_id = %s order by m.created_at desc",
            (user_id,),
        )
    ]
    recordings = [
        {"title": title, "subject": code, "status": status}
        for title, code, status in conn.execute(
            "select coalesce(l.title, l.audio_key), l.subject_code, l.status "
            "from lectures l where l.uploader_id = %s order by l.recorded_at desc",
            (user_id,),
        )
    ]
    return {
        "points": dict(zip(("uploads", "recordings", "votes_received", "score"), row)),
        "uploads": uploads,
        "recordings": recordings,
    }


# How much of the board a phone is sent. The rest of the class is not withheld
# so much as not useful: nobody scrolls to 74th, and the one row past the top
# that matters -- your own -- is fetched by name whatever your rank.
BOARD_TOP = 20

# The two windows the board offers, and the columns each one reads. Written out
# rather than built by string surgery, because these are identifiers going
# straight into SQL and there is nothing to interpolate but these.
BOARD_WINDOWS = {
    "all": ("uploads", "recordings", "votes_received", "score", "rank"),
    "week": ("week_uploads", "week_recordings", "week_votes", "week_score",
             "week_rank"),
}


def db_standings(conn, user_id, top=BOARD_TOP):
    """The class board: the top of it, and always the person reading it.

    Both decisions are Postgres's. rank() ranks, the where-clause trims, and
    this function never sees a row it is not going to print -- which is the
    whole reason standings is a view with a window function in it rather than a
    sort in Python over a hundred and ten profiles.

    Nobody with nothing yet is on the board. On day one that is everybody, and
    a hundred and ten rows of zero is not a ranking, it is a class list. The
    person asking is the one exception, so `you` is an answer even before they
    have earned a point -- that is what "your position, always visible" means.
    """
    out = {}
    for window, (up, rec, vot, score, rank) in BOARD_WINDOWS.items():
        rows = [
            {"id": str(i), "name": n, "role": r, "uploads": u, "recordings": c,
             "votes_received": v, "score": s, "rank": k, "you": str(i) == str(user_id)}
            for i, n, r, u, c, v, s, k in conn.execute(
                f"select id, name, role, {up}, {rec}, {vot}, {score}, {rank} "
                f"from standings where ({score} > 0 and {rank} <= %s) or id = %s "
                f"order by {rank}, name",
                (top, user_id),
            )
        ]
        # Splitting a list of at most twenty-one rows is not ranking; the rank
        # came off the view. The board and the pinned row are separate because
        # a viewer at 74th appended to the end of the top twenty reads as 21st.
        out[window] = {
            "top": [r for r in rows if r["score"] > 0 and r["rank"] <= top],
            "you": next((r for r in rows if r["you"]), None),
        }
    return out


# ---- The notice board. -------------------------------------------------
#
# How much of it a phone is sent. A section posts a handful of notices a term,
# so thirty is every one of them that matters and a cap on the day somebody
# pastes the whole timetable in one line at a time.
ANN_LIMIT = 30
ANN_TITLE = 120
ANN_BODY = 4000


def db_announcements(conn, user_id, limit=ANN_LIMIT):
    """The board as one person sees it: pinned first, then newest.

    Read as the caller, so the policies decide what comes back rather than this
    query: a hidden notice reaches the admin who wrote it and nobody else, and
    `unread` is that person's own read mark and never anybody else's.

    `deleted` rides along because the one person who can see a hidden notice is
    the one who might want it back.
    """
    return [
        {"id": str(i), "title": t, "body": b, "pinned": pin, "by": by,
         "at": at, "edited": ed, "mine": mine, "unread": unread, "deleted": gone}
        for i, t, b, pin, by, at, ed, mine, unread, gone in conn.execute(
            "select a.id, a.title, a.body, a.pinned, p.name, "
            "  extract(epoch from a.created_at)::bigint, "
            "  extract(epoch from a.updated_at)::bigint, "
            "  a.author_id = %(me)s, r.profile_id is null, a.deleted_at is not null "
            "from announcements a "
            "join profiles p on p.id = a.author_id "
            "left join announcement_reads r "
            "  on r.announcement_id = a.id and r.profile_id = %(me)s "
            "order by a.pinned desc, a.created_at desc limit %(limit)s",
            {"me": user_id, "limit": limit},
        )
    ]


def db_write_announcement(conn, user_id, aid, title, body, pinned, deleted):
    """Post one, edit one, hide one, or put a hidden one back.

    One function because it is one row and one policy: "class reps edit their own"
    is what refuses somebody else's notice, and it refuses it here whether the
    request came from this app or from curl holding a stolen cookie. A hidden
    notice is never destroyed -- deleted_at is the only thing that moves, and
    there is no delete policy on the table at all.

    Returns the row as the board will show it, so the screen redraws from what
    was stored rather than from what was typed.
    """
    title = (title or "").strip()[:ANN_TITLE]
    body = (body or "").strip()[:ANN_BODY]
    if aid:
        if deleted is None:
            if not title:
                raise ValueError("an announcement needs a title")
            done = conn.execute(
                "update announcements set title = %s, body = %s, pinned = %s, "
                "updated_at = now() where id = %s returning id",
                (title, body, bool(pinned), aid))
        else:
            # now() rather than a timestamp from this process: the row's
            # created_at is Postgres's clock, and two clocks on one row is how
            # a notice ends up hidden a minute before it was written.
            done = conn.execute(
                "update announcements set deleted_at = case when %s then now() end "
                "where id = %s returning id", (bool(deleted), aid))
        if not done.fetchone():
            raise ValueError("no such announcement, or it is not yours")
        return aid
    if not title:
        raise ValueError("an announcement needs a title")
    return str(conn.execute(
        "insert into announcements (author_id, title, body, pinned) "
        "values (%s, %s, %s, %s) returning id",
        (user_id, title, body, bool(pinned))).fetchone()[0])


def db_mark_read(conn, user_id, ids):
    """Mark notices read for one person, on the server where every device of
    theirs can see it.

    `on conflict do nothing`: the phone sends what is on screen, and what is on
    screen is often something they have already read on the laptop.
    """
    ids = [str(i) for i in ids][:ANN_LIMIT]
    if not ids:
        return
    conn.execute(
        "insert into announcement_reads (announcement_id, profile_id) "
        "select id, %s from announcements where id = any(%s::uuid[]) "
        "on conflict do nothing", (user_id, ids))


# ---------------------------------------------------------------------------
# CAMPUS: the societies, what they are running, and where anything is.
#
# Three tables and one read. Campus is one screen and it is opened once, on
# mobile data, between classes -- three round trips for three lists is three
# things that can be slow, so /campus answers all of it at once, exactly the
# way /data already carries Home's extras.

CLUB_NAME = 80
EVENT_TITLE = 120
BLURB = 600
TAG_LIMIT = 8


def slugify(name, existing=()):
    """A stable, readable key from a name, or a reason it is not one.

    The seed file writes slugs by hand; this is for the ones an admin types on
    a phone. Suffixed until it is free, so adding a second "Robotics Club"
    renames rather than overwrites the first one's row.
    """
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:48]
    if len(base) < 2:
        raise ValueError("that name has no letters or digits in it")
    slug, n = base, 1
    while slug in existing:
        n += 1
        slug = f"{base[:44]}-{n}"
    return slug


def db_clubs(conn):
    """The directory, as the tab shows it: A to Z, hidden ones only for the
    admin whose policy lets them through."""
    return [
        {"slug": s, "name": n, "blurb": b, "category": c, "tags": list(t or []),
         "link": link, "contact": contact, "hidden": hidden}
        for s, n, b, c, t, link, contact, hidden in conn.execute(
            "select slug, name, blurb, category, tags, link, contact, hidden "
            "  from clubs order by name")
    ]


def db_write_club(conn, slug, name, blurb, category, tags, link, contact, hidden):
    """Add one, edit one, or hide one. Admins only -- and that is the policy's
    ruling, not this function's: it runs on the caller's own connection."""
    name = (name or "").strip()[:CLUB_NAME]
    if slug and hidden is not None and not name:
        # Hiding and unhiding is the one edit that does not resend the rest.
        done = conn.execute("update clubs set hidden = %s where slug = %s "
                            "returning slug", (bool(hidden), slug))
        if not done.fetchone():
            raise ValueError("no such club")
        return slug
    if not name:
        raise ValueError("a club needs a name")
    fields = (name, (blurb or "").strip()[:BLURB], (category or "").strip()[:40],
              [str(t).strip()[:40] for t in (tags or []) if str(t).strip()][:TAG_LIMIT],
              (link or "").strip()[:300] or None, (contact or "").strip()[:120] or None,
              bool(hidden))
    if slug:
        done = conn.execute(
            "update clubs set name = %s, blurb = %s, category = %s, tags = %s, "
            "link = %s, contact = %s, hidden = %s where slug = %s returning slug",
            fields + (slug,))
        if not done.fetchone():
            raise ValueError("no such club")
        return slug
    taken = {s for (s,) in conn.execute("select slug from clubs")}
    slug = slugify(name, taken)
    conn.execute(
        "insert into clubs (slug, name, blurb, category, tags, link, contact, hidden) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s)", (slug,) + fields)
    return slug


def _event_date(raw, what):
    """One date off the wire, or a reason it is not one."""
    if raw in (None, ""):
        return None
    try:
        return datetime.date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        raise ValueError(f"{what} is not a date")


def db_events(conn, user_id=None, ahead=None):
    """What is still coming, soonest first.

    Past events fall off here rather than by anybody tidying up: the filter is
    on the day it ends, so a three-day fest stays on the list through its last
    day and is gone the morning after. Nothing is deleted for it.
    """
    rows = conn.execute(
        "select id, title, society, starts_on, ends_on, venue, blurb, "
        "       deleted_at is not null, added_by "
        "  from events "
        " where coalesce(ends_on, starts_on) >= current_date "
        " order by starts_on, title")
    out = []
    for eid, title, society, starts, ends, venue, blurb, gone, by in rows:
        out.append({"id": str(eid), "title": title, "society": society,
                    "mine": str(by) == str(user_id),
                    "date": starts.isoformat(),
                    "ends": (ends or starts).isoformat(),
                    "multi": bool(ends and ends != starts),
                    "venue": venue, "blurb": blurb, "deleted": gone})
    return out[:ahead] if ahead else out


def db_write_event(conn, user_id, eid, title, society, starts, ends, venue,
                   blurb, deleted):
    """Add one, edit one, or take one down.

    Trusted adds; the policy decides that, and an admin's reach over somebody
    else's event is the same policy's business. Taking one down sets deleted_at
    and leaves the row, like every other thing on this tab that people may have
    already read.
    """
    if eid and deleted is not None:
        done = conn.execute(
            "update events set deleted_at = case when %s then now() end "
            "where id = %s returning id", (bool(deleted), eid))
        if not done.fetchone():
            raise ValueError("no such event, or it is not yours")
        return eid
    title = (title or "").strip()[:EVENT_TITLE]
    if not title:
        raise ValueError("an event needs a title")
    start = _event_date(starts, "the start date")
    if not start:
        raise ValueError("an event needs a date -- that is the whole point of one")
    end = _event_date(ends, "the end date")
    if end and end < start:
        raise ValueError("that event ends before it starts")
    fields = (title, (society or "").strip()[:CLUB_NAME], start, end,
              (venue or "").strip()[:120], (blurb or "").strip()[:BLURB])
    if eid:
        done = conn.execute(
            "update events set title = %s, society = %s, starts_on = %s, "
            "ends_on = %s, venue = %s, blurb = %s where id = %s returning id",
            fields + (eid,))
        if not done.fetchone():
            raise ValueError("no such event, or it is not yours")
        return eid
    return str(conn.execute(
        "insert into events (title, society, starts_on, ends_on, venue, blurb, "
        "added_by) values (%s, %s, %s, %s, %s, %s, %s) returning id",
        fields + (user_id,)).fetchone()[0])


PLACE_KINDS = ("academic", "hostel", "food", "sport", "admin", "gate",
               "health", "other")


def db_places(conn):
    """Every landmark, grouped by the screen rather than here -- one order, by
    kind and then by name, so the list reads the same whether or not a map
    ever draws above it."""
    return [
        {"slug": s, "name": n, "kind": k, "lat": lat, "lng": lng,
         "approx": approx, "note": note, "hidden": hidden}
        for s, n, k, lat, lng, approx, note, hidden in conn.execute(
            "select slug, name, kind, lat, lng, approx, note, hidden "
            "  from places order by kind, name")
    ]


def db_papers(conn, code=None):
    """Every archived document for one subject, newest first -- or, with no
    code, the whole archive at once for the cross-subject papers screen.

    The shelf the seniors' portal kept in 31 folder names, read back as four
    columns. Ordering is year descending with nulls last, because a paper
    nobody dated is still worth showing -- just not above this year's.

    No section clause, and that is the design rather than an omission: 0050
    put this table beside clubs and places instead of inside `materials`
    precisely because every section sat the same End Term.
    """
    # The subject screen groups by exam under one course, so it wants the
    # course's own order; the papers screen groups by exam across all of them
    # and wants the newest sitting first whatever course set it.
    where = "" if code is None else "where subject_code = %s "
    args = () if code is None else (code,)
    order = ("order by year desc nulls last, subject_code, title"
             if code is None else
             "order by year desc nulls last, kind, exam, title")
    return [
        {"id": str(i), "subject_code": sc, "kind": k, "exam": e, "year": y,
         "section": sec, "title": t, "path": key, "bytes": n}
        for i, sc, k, e, y, sec, t, key, n in conn.execute(
            "select id, subject_code, kind, exam, year, set_for_section, "
            "       title, file_key, size_bytes "
            "  from archive_documents " + where + order, args)
    ]


def db_paper_counts(conn):
    """{subject_code: how many papers}, for the screen that lists subjects.

    A count rather than the rows: that screen prints one number per course and
    would otherwise pull the whole archive -- 296 rows to render twelve
    subtitles -- on a phone that only wanted to know whether opening the
    subject is worth it. The rows themselves still come from /papers, one
    course at a time, when a course is actually opened.

    No section clause, for the same reason db_papers has none: every section
    sat the same End Term.
    """
    return {code: n for code, n in conn.execute(
        "select subject_code, count(*) from archive_documents "
        "group by subject_code")}


def db_write_place(conn, slug, name, kind, lat, lng, approx, note):
    """Add or edit one pin. Admins only, by policy."""
    name = (name or "").strip()[:CLUB_NAME]
    if not name:
        raise ValueError("a place needs a name")
    kind = (kind or "other").strip()
    if kind not in PLACE_KINDS:
        raise ValueError(f"{kind!r} is not one of {', '.join(PLACE_KINDS)}")

    def coord(raw, what, limit):
        if raw in (None, ""):
            return None
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{what} is not a number")
        if not -limit <= v <= limit:
            raise ValueError(f"{what} is not on Earth")
        return v

    fields = (name, kind, coord(lat, "the latitude", 90),
              coord(lng, "the longitude", 180),
              True if approx is None else bool(approx),
              (note or "").strip()[:200])
    if slug:
        done = conn.execute(
            "update places set name = %s, kind = %s, lat = %s, lng = %s, "
            "approx = %s, note = %s where slug = %s returning slug",
            fields + (slug,))
        if not done.fetchone():
            raise ValueError("no such place")
        return slug
    taken = {s for (s,) in conn.execute("select slug from places")}
    slug = slugify(name, taken)
    conn.execute(
        "insert into places (slug, name, kind, lat, lng, approx, note) "
        "values (%s, %s, %s, %s, %s, %s, %s)", (slug,) + fields)
    return slug


def db_remove_place(conn, slug):
    """Actually gone, unlike everything else on this tab. A pin is nobody's
    words -- removing one destroys nothing anybody wrote."""
    if not conn.execute("delete from places where slug = %s returning slug",
                        (slug,)).fetchone():
        raise ValueError("no such place")


# The campus, boxed. The map is bounded to these corners so a dragged finger
# cannot wander off to the other side of Bhopal and leave a first-year looking
# at a lake. Approximate on purpose and generous by about a hundred metres --
# a box that is slightly too big shows a road you can walk in on; one that is
# slightly too small clips a hostel.
CAMPUS_BOUNDS = {"south": 23.2115, "west": 77.4020,
                 "north": 23.2225, "east": 77.4125}
CAMPUS_CENTRE = {"lat": 23.2170, "lng": 77.4075}


def maps_config(env=os.environ):
    """What the phone needs to draw a map, or an honest nothing.

    The key is read from the environment the same way session_secret() and
    worker_token() read theirs, and unlike those two it is NEVER minted: there
    is no such thing as a provider key this process can invent. It is also
    never baked into a static export -- `notes.py export` writes PAGE with no
    server behind it, and a key in that file is a key in the repository.

    No key is a first-class state, not a failure. The places list is the useful
    half of this screen and it needs no provider at all, so the page draws it
    either way and says plainly why the map above it is missing.
    """
    key = (env.get("NEXT_PUBLIC_GOOGLE_MAPS_API_KEY") or "").strip()
    # One small adapter, named here and implemented in the page. Both were
    # asked for and neither key exists yet, so the provider is a string in the
    # environment rather than a decision baked into the JavaScript.
    provider = (env.get("RECARVE_MAPS_PROVIDER") or "google").strip().lower()
    return {"provider": provider if key else None,
            "key": key or None,
            "bounds": CAMPUS_BOUNDS, "centre": CAMPUS_CENTRE}


DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
PERIODS = 8

# Monday is 1 and Saturday is 6 -- the numbering section_timetable already
# checks for. Sunday is in DAYS because a week has one, and is not taught.
TEACHING_DAYS = {day.lower(): n for n, day in enumerate(DAYS) if 1 <= n <= 6}


def parse_timetable(text, known_codes):
    """A photographed timetable, retyped as CSV, read into rows and complaints.

    Pure on purpose: text in, ``(rows, errors)`` out, no database and no
    printing, so the CLI and the /super page can both call it and the tests
    need neither. `known_codes` is a parameter for the same reason -- today the
    caller passes every subject there is, and once a section points at a
    subject set it passes that set's, without this function ever learning what
    a section is.

    rows are ``(day, period, code)``, sorted, day 1=Monday..6=Saturday. errors
    are sentences naming the line they are about, and there is one for every
    bad line: somebody pasting forty rows should learn all forty mistakes in
    one go rather than one per attempt.

    Forgiving about what a paste mangles -- the header row or its absence,
    blank lines, CRLF, stray whitespace, trailing commas, and the curly quotes
    Excel and chat windows substitute. Unforgiving about what changes the
    meaning -- an unknown code, a period off the grid, a day it cannot read,
    and the same (day, period) filled twice. A silently wrong timetable sends a
    hundred and ten people to the wrong room; a rejected one sends nobody
    anywhere.
    """
    codes = {code.upper(): code for code in known_codes}
    rows, line_of, errors = {}, {}, []
    text = (text.lstrip("﻿")
                .replace("“", '"').replace("”", '"')
                .replace("‘", "'").replace("’", "'"))
    for n, line in enumerate(text.splitlines(), 1):
        try:
            # csv unwraps "quoted" cells; the second strip is for the quotes it
            # does not treat as quotes -- an apostrophe, or a lone unbalanced one.
            cells = [c.strip().strip("'\"").strip() for c in next(csv.reader([line]), [])]
        except csv.Error as e:
            errors.append(f"line {n}: cannot read this line ({e})")
            continue
        while cells and not cells[-1]:              # trailing commas
            cells.pop()
        if not cells:                               # blank, or a row of commas
            continue
        if cells[0].lower() == "day":               # the header, if there is one
            continue
        if len(cells) != 3:
            errors.append(
                f"line {n}: expected day,period,subject_code -- got {line.strip()!r}")
            continue
        day_name, period_text, code = cells
        day = TEACHING_DAYS.get(day_name.lower())
        if day is None:
            errors.append(f"line {n}: {day_name!r} is not a day from Monday to Saturday")
        try:
            period = int(period_text)
        except ValueError:
            errors.append(f"line {n}: period {period_text!r} is not a number")
            period = None
        if period is not None and not 1 <= period <= PERIODS:
            errors.append(f"line {n}: period {period} is not 1 to {PERIODS}")
            period = None
        if code.upper() not in codes:
            errors.append(f"line {n}: unknown subject code {code!r}")
            continue
        if day is None or period is None:
            continue
        if (day, period) in rows:
            errors.append(f"line {n}: {DAYS[day]} period {period} is already "
                          f"{rows[(day, period)]}, set on line {line_of[(day, period)]}")
            continue
        rows[(day, period)] = codes[code.upper()]
        line_of[(day, period)] = n
    if not rows and not errors:
        errors.append("no timetable rows found")
    return [(day, period, code) for (day, period), code in sorted(rows.items())], errors


def db_timetable(conn, user_id):
    """One student's week, in the order Home reads it.

    Seeded from the section's template when the profile is made, and written
    by nothing else: the grid is the registrar's, one per section, and a
    student editing their own copy of it only ever drifted away from the real
    one. An empty week means nobody has put that section's grid in yet.
    """
    return [
        {"day": day, "period": period, "code": code}
        for day, period, code in conn.execute(
            "select day, period, subject_code from timetable "
            "where profile_id = %s order by day, period",
            (user_id,),
        )
    ]


# ---- Attendance. ---------------------------------------------------------
# MANIT wants 75% in EACH subject, not 75% overall, so every number here is
# per subject. Overall attendance would be a comforting average that nobody is
# ever refused an exam over.
#
# Three states and only two of them are rows: present and absent are written
# down, and not-yet-marked is no row. Nothing here ever assumes present. An
# unmarked period is in neither the numerator nor the denominator, which means
# a student who has marked nothing sees "no classes marked yet" rather than a
# confident 100%.

ATT_STATES = ("present", "absent")
# How far back the catch-up screen can reach in one payload. Four weeks is more
# than anybody ever has to catch up on, and it keeps /data -- which the page
# refetches after every vote -- to a couple of hundred small rows.
ATT_WINDOW = 28
# The threshold, as a fraction with integer parts. Every sum below is done in
# whole numbers against these two, so nothing turns on a float comparing equal.
NEED_NUM, NEED_DEN = 3, 4          # 3/4 = 75%


def attendance_maths(attended, held):
    """The whole of the arithmetic, in one place, in integers.

    `pct` is floored to one decimal and never rounded. 74.96% is not 75%, and a
    number that rounds up across the threshold is the one lie this screen must
    not tell -- a student would read it as safe and be refused the exam.

    `can_miss` is the largest k with attended / (held + k) >= 3/4, i.e. how many
    of the next classes in a row may be missed. It deliberately assumes nothing
    about how many classes the semester holds: nobody has told this app when
    the semester ends, so "you can miss 7 more this term" would be fiction.
    What IS knowable from the data is the run of consecutive misses, and that is
    what is said.

    `must_attend` is the mirror for somebody already below: the smallest n with
    (attended + n) / (held + n) >= 3/4. Also free of any semester total.

        4a >= 3(h + k)        ->  k = floor((4a - 3h) / 3)
        4(a + n) >= 3(h + n)  ->  n = 3h - 4a
    """
    a, h = int(attended), int(held)
    ok = NEED_DEN * a >= NEED_NUM * h          # at or above 75%, exactly
    return {
        "attended": a,
        "held": h,
        # Floor, not round: attended*1000//held is tenths of a percent, dropped
        # rather than nudged. 0 held has no percentage at all, and None says so
        # instead of a 0% that reads as a failing student.
        "pct": None if not h else (a * 1000 // h) / 10,
        "ok": ok,
        "can_miss": max(0, (NEED_DEN * a - NEED_NUM * h) // NEED_NUM),
        "must_attend": max(0, NEED_NUM * h - NEED_DEN * a),
    }


def attendance_note(m):
    """The consequence, in words, from the numbers above and nothing else.

    Said on the server so the phone, the tests and any future screen all read
    the same sentence, and so no screen can invent a cheerier one.

    Calm on purpose. Below 75% is a number and a next step, not an alarm: the
    student already knows it is bad, and what they need from this line is the
    count of classes that fixes it.
    """
    if not m["held"]:
        return "No classes marked yet."
    if not m["ok"]:
        n = m["must_attend"]
        return ("Attend the next class to get back to 75%." if n == 1 else
                f"Attend the next {n} classes in a row to get back to 75%.")
    k = m["can_miss"]
    if not k:
        return "Miss the next class and you drop below 75%."
    return ("You can miss one more class and stay at 75%." if k == 1 else
            f"You can miss the next {k} classes and stay at 75%.")


def db_attendance(conn, user_id, window=ATT_WINDOW):
    """Everything the phone needs about attendance, on one read.

    The per-subject totals cover every mark ever made; the marks and the
    cancellations cover the last four weeks, which is what the catch-up screen
    can reach. `today` is this machine's date rather than the handset's, for
    the same reason /data already sends `now`: a phone whose clock is a day out
    would otherwise offer to mark tomorrow.
    """
    today = conn.execute("select current_date").fetchone()[0]
    # A cancelled class counts for nobody, so it leaves both sums -- the
    # anti-join, not a filter on state. Rows stay: a cancellation can be undone
    # and the mark underneath it is still what the student said.
    totals = {
        code: (present, held)
        for code, present, held in conn.execute(
            "select a.subject_code, "
            "       count(*) filter (where a.state = 'present')::int, "
            "       count(*)::int "
            "  from attendance a "
            "  left join cancelled_classes c "
            "    on c.on_date = a.on_date and c.period = a.period "
            "   and c.subject_code = a.subject_code "
            " where a.profile_id = %s and c.on_date is null "
            " group by a.subject_code", (user_id,))
    }
    # Every subject the student actually has, not only the ones they have
    # marked -- a subject at 0 held has to be able to say "no classes marked
    # yet" rather than be missing from the screen entirely.
    codes = {c for (c,) in conn.execute(
        "select distinct subject_code from timetable where profile_id = %s",
        (user_id,))} | set(totals)
    order = list(SUBJECTS)
    subjects = []
    for code in sorted(codes, key=lambda c: order.index(c) if c in SUBJECTS else 99):
        m = attendance_maths(*totals.get(code, (0, 0)))
        m["code"] = code
        m["note"] = attendance_note(m)
        subjects.append(m)
    return {
        "today": today.isoformat(),
        "window": window,
        "subjects": subjects,
        "marks": [
            {"date": d.isoformat(), "period": p, "code": code, "state": state}
            for d, p, code, state in conn.execute(
                "select on_date, period, subject_code, state from attendance "
                "where profile_id = %s and on_date > current_date - %s "
                "order by on_date, period", (user_id, window))
        ],
        # Class-wide, so this is the same list for everybody and is read by
        # every member: a denominator that changed has to be able to say why.
        "off": [
            {"date": d.isoformat(), "period": p, "code": code, "reason": reason}
            for d, p, code, reason in conn.execute(
                "select on_date, period, subject_code, reason from cancelled_classes "
                "where on_date > current_date - %s order by on_date, period",
                (window,))
        ],
        # Days the institute already said would hold no class, expanded to one
        # entry per date so the phone can answer "is anything on?" with a lookup
        # rather than by re-implementing range arithmetic.
        #
        # Separate from `off` on purpose, though both empty a day. A
        # cancellation is something a trusted member did last Tuesday and can
        # undo; a holiday is something the institute published in July. They
        # read differently on the screen and they are answerable by different
        # people.
        #
        # Sunday is not in here. The timetable has no Sunday rows, so a Sunday
        # is already empty for the only reason that matters, and announcing
        # "Sunday: no classes" as though it were news helps nobody.
        #
        # Reaches further forward than back: the day view steps forward, and
        # walking into next week's mid-sem break should say so.
        "closed": [
            {"date": d.isoformat(), "title": title, "kind": kind}
            for d, title, kind in conn.execute(
                "select g::date, c.title, c.kind "
                "  from generate_series(current_date - %s::int, "
                "                       current_date + 21, interval '1 day') g "
                "  join academic_calendar c "
                "    on not c.teaching and g::date between c.starts_on and c.ends_on "
                " order by g", (window,))
        ],
        # The next thing worth counting down to. `ends_on >= current_date` so a
        # window already running still names itself -- during mid-term week the
        # honest line is "mid-terms, on now", not the date they finish.
        "next": next(
            ({"date": s.isoformat(), "ends": e.isoformat(), "title": t, "kind": k}
             for s, e, t, k in conn.execute(
                 "select starts_on, ends_on, title, kind from academic_calendar "
                 " where notable and ends_on >= current_date "
                 " order by starts_on limit 1")),
            None),
        # The next several, for Home's calendar: `next` stays for anything
        # that only ever wanted the one.
        #
        # One list, two sources. A student does not keep two calendars -- the
        # mid-term window and Tooryanaad are both "what is coming", and asking
        # them to check Home for one and Campus for the other is how the second
        # one gets missed. `what` is what keeps it honest: every row says which
        # kind of thing it is, so a fest is never mistaken for an exam.
        #
        # A past event falls off here by itself: the filter is the day it ends,
        # so a three-day fest survives its own last day and is gone the morning
        # after without anybody tidying anything.
        "upcoming": [
            {"date": s.isoformat(), "ends": e.isoformat(), "title": t,
             "kind": k, "what": what, "where": where}
            for s, e, t, k, what, where in conn.execute(
                "select starts_on, ends_on, title, kind, 'academic', '' "
                "  from academic_calendar "
                " where notable and ends_on >= current_date "
                "union all "
                "select starts_on, coalesce(ends_on, starts_on), title, "
                "       'event', 'campus', "
                "       nullif(concat_ws(' \u00b7 ', nullif(society, ''), "
                "                        nullif(venue, '')), '') "
                "  from events "
                " where deleted_at is null "
                "   and coalesce(ends_on, starts_on) >= current_date "
                " order by 1 limit 6")],
    }


def _att_date(raw, today, window):
    """One date off the wire, or a reason it is not one."""
    try:
        day = datetime.date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        raise ValueError(f"{raw!r} is not a date")
    if day > today:
        raise ValueError("that class has not happened yet")
    if (today - day).days > window:
        raise ValueError("that is further back than this app keeps")
    return day


def db_mark_attendance(conn, user_id, marks):
    """Write one tap, or a whole week of them. Returns rows changed.

    A list rather than a single mark because catching up on a missed week is
    the case this has to be fast for: "everything on Tuesday, present" is one
    request and one transaction, not seven.

    Retroactive by design -- people forget, and a marking screen that only
    worked today would be filled in by nobody. The past is bounded by the same
    window the read uses; the future is refused outright.

    The subject comes from the student's own timetable, never from the request.
    That is what stops a mark being filed under a subject the student was not
    sitting in, and it is why a date with no class scheduled is refused rather
    than silently written down.
    """
    today = conn.execute("select current_date").fetchone()[0]
    clean = {}
    for m in marks:
        try:
            period = int(m["period"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("a mark needs a date and a period")
        if not 1 <= period <= PERIODS:
            raise ValueError(f"period {period} is not 1 to {PERIODS}")
        state = m.get("state")
        if state not in ATT_STATES and state != "clear":
            raise ValueError(f"{state!r} is not present, absent or clear")
        clean[(_att_date(m.get("date"), today, ATT_WINDOW), period)] = state

    with conn.transaction():
        for (day, period), state in sorted(clean.items()):
            if state == "clear":
                # Back to not-yet-marked, which is a real state and the one a
                # mistap has to be able to get back to.
                conn.execute(
                    "delete from attendance where profile_id = %s "
                    "and on_date = %s and period = %s", (user_id, day, period))
                continue
            row = conn.execute(
                "select subject_code from timetable where profile_id = %s "
                "and day = extract(isodow from %s::date)::int and period = %s",
                (user_id, day, period)).fetchone()
            if not row:
                raise ValueError(
                    f"you have no class in period {period} on {day.isoformat()}")
            conn.execute(
                "insert into attendance (profile_id, on_date, period, "
                "subject_code, state) values (%s, %s, %s, %s, %s) "
                "on conflict (profile_id, on_date, period) do update set "
                "state = excluded.state, subject_code = excluded.subject_code, "
                "marked_at = now()",
                (user_id, day, period, row[0], state))
    return len(clean)


def db_set_cancelled(conn, user_id, day, period, code, off, reason=""):
    """Call one class off for everybody, or put it back. Trusted only.

    Trusted is enforced by the policy, not here: this runs on the caller's own
    connection, so a student who reaches this function at all is refused by the
    database.
    """
    today = conn.execute("select current_date").fetchone()[0]
    try:
        day = datetime.date.fromisoformat(str(day))
    except (TypeError, ValueError):
        raise ValueError(f"{day!r} is not a date")
    # Bounded the same way a mark is, except forwards too: calling off
    # tomorrow's lecture is the useful case, and a week's notice is as much as
    # anybody ever gives.
    if (day - today).days > 7:
        raise ValueError("that is further ahead than this app keeps")
    if (today - day).days > ATT_WINDOW:
        raise ValueError("that is further back than this app keeps")
    try:
        period = int(period)
    except (TypeError, ValueError):
        raise ValueError("a class needs a period")
    if not 1 <= period <= PERIODS:
        raise ValueError(f"period {period} is not 1 to {PERIODS}")
    if code not in SUBJECTS:
        raise ValueError(f"unknown subject {code!r}")
    if off:
        conn.execute(
            "insert into cancelled_classes (on_date, period, subject_code, "
            "reason, set_by) values (%s, %s, %s, %s, %s) "
            # The section is in the key as of 0041 -- two sections both call
            # off their own 9am and neither may be refused because the other
            # did it first -- and ON CONFLICT has to name the whole key. The
            # insert still does not mention section_id: it defaults to the
            # caller's own, which is the only one the policy would accept.
            "on conflict (section_id, on_date, period, subject_code) do nothing",
            (day, period, code, (reason or "")[:120], user_id))
    else:
        conn.execute(
            "delete from cancelled_classes where on_date = %s and period = %s "
            "and subject_code = %s", (day, period, code))
    return {"date": day.isoformat(), "period": period, "code": code,
            "off": bool(off)}


def db_backfill(conn, library):
    """Register whatever is already on disk, once, in the admin's name.

    Everything here predates the database and somebody has to own it. The admin
    is the only account certain to exist, and this runs as them rather than as
    the table owner, so the same policies apply as to any other upload.
    """
    row = conn.execute(
        "select id from profiles where role = 'admin' and status = 'approved' "
        "order by created_at limit 1"
    ).fetchone()
    if not row:
        return 0                     # nobody has joined; nothing to attribute to
    admin = str(row[0])
    act_as(conn, admin)
    mats, lecs = db_meta(conn, admin)
    added = 0
    for code, (name, _) in SUBJECTS.items():
        folder = Path(library) / f"{code}-{name}"
        lectures, uploads = folder / "lectures", folder / "uploads"
        for md in sorted(lectures.glob("*.md")) if lectures.is_dir() else []:
            if (code, md.stem) in lecs:
                continue
            conn.execute(
                "insert into lectures (subject_code, uploader_id, title, audio_key, "
                "status) values (%s, %s, %s, %s, 'done')",
                (code, admin, md.stem, str(md)),
            )
            added += 1
        for f in sorted(uploads.glob("*")) if uploads.is_dir() else []:
            if not f.is_file() or (code, f.name) in mats:
                continue
            conn.execute(
                "insert into materials (subject_code, uploader_id, filename, file_key, "
                "size_bytes) values (%s, %s, %s, %s, %s)",
                (code, admin, f.name, str(f), f.stat().st_size),
            )
            added += 1
    act_as(conn, None)
    return added


def db_bootstrap(conn):
    """The way in, on an empty database. Returns a usable invite code, or None
    once somebody has joined and can hand out their own."""
    if conn.execute("select count(*) from profiles").fetchone()[0]:
        return None
    row = conn.execute(
        "select code from invites where expires_at > now() and uses < max_uses limit 1"
    ).fetchone()
    if row:
        return row[0]
    code = os.environ.get("RECARVE_BOOTSTRAP_INVITE") or secrets.token_hex(4)
    conn.execute(
        "insert into invites (code, expires_at) values (%s, now() + interval '30 days') "
        "on conflict (code) do nothing",
        (code,),
    )
    return code


# ---- What the super admin reads and writes. ------------------------------
#
# Every function here runs on a connection with no user set -- the one that
# owns the tables, which row level security does not apply to. That is not a
# hole in 0042, it is the other side of it: a super admin is not in any
# section, so there is no my_section() for a policy to compare against, and
# 0040 says so in as many words ("creating one is the super admin's, on a
# connection that owns these tables"). Nothing below drops, weakens or adds a
# policy, and no student session ever reaches any of it -- the handler opens
# this connection only after /super's own cookie has verified.
#
# The section clause a policy would have added is therefore written out by
# hand in each of these, keyed on the section the screen is open on. That is
# the thing to check when editing them.


class SuperRefused(ValueError):
    """A refusal with a list attached: every bad line at once.

    A ValueError because that is already what the db_ functions raise for
    "a person typed something wrong", and the handler answers all of them the
    same way. `lines` is the part a plain ValueError has nowhere to put --
    learning one mistake per attempt is how a forty-line paste takes an hour.
    """

    def __init__(self, said, lines):
        super().__init__(said)
        self.lines = list(lines)


def a_uuid(raw):
    """`raw` as a uuid string, or a sentence about why it is not one.

    Every id /super is handed comes back off its own screen, so this is not
    input validation so much as the difference between a 400 and a 500: a
    ::uuid cast on junk is a psycopg DataError halfway through a handler.
    """
    try:
        return str(uuid.UUID(str(raw)))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("that is not something this screen can act on")


def section_label(name, grad_year):
    """'Section I '30'. The one place the display form is spelled."""
    return f"Section {name} '{int(grad_year) % 100:02d}"


def db_sections(conn):
    """Every section there is, with the count that says whether anybody is in
    it yet. The whole list, on purpose: this is the one screen that is above
    the section line rather than inside it."""
    return [
        {"id": str(r[0]), "name": r[1], "grad_year": r[2],
         "label": section_label(r[1], r[2]), "set": r[3], "set_id": str(r[4]) if r[4] else None,
         "members": r[5], "invite": r[6]}
        for r in conn.execute(
            "select s.id, s.name, s.grad_year, ss.name, ss.id,"
            "  (select count(*) from profiles p where p.section_id = s.id),"
            "  (select i.code from invites i where i.section_id = s.id"
            "    and i.expires_at > now() and i.uses < i.max_uses"
            "    order by i.expires_at desc limit 1)"
            " from sections s left join subject_sets ss on ss.id = s.subject_set_id"
            " order by s.grad_year, s.name"
        )
    ]


def db_subject_sets(conn):
    """The curriculums a new section can be pointed at."""
    return [
        {"id": str(r[0]), "name": r[1], "subjects": r[2]}
        for r in conn.execute(
            "select ss.id, ss.name,"
            " (select count(*) from subject_set_members m where m.set_id = ss.id)"
            " from subject_sets ss order by ss.name"
        )
    ]


def db_create_section(conn, name, grad_year, subject_set_id):
    """A new section. Returns the row the list screen would show for it.

    Validated here rather than left to the constraints, because "duplicate key
    value violates unique constraint" is not a sentence to put in front of
    somebody who typed the year wrong.
    """
    # A section name is a label, not a filename -- 'I', 'II', 'A'. Whitespace
    # collapsed so "I " and "I" cannot both exist under a unique key that
    # thinks they are different.
    name = " ".join(str(name or "").split())[:20]
    if not name:
        raise ValueError("a section needs a name")
    try:
        grad_year = int(grad_year)
    except (TypeError, ValueError):
        raise ValueError("the graduation year has to be a number")
    # Wide enough for anything anybody is graduating, narrow enough that a
    # mistyped 20230 is refused rather than filed.
    if not 2000 <= grad_year <= 2100:
        raise ValueError("the graduation year has to be between 2000 and 2100")
    if not conn.execute("select 1 from subject_sets where id = %s::uuid",
                        (subject_set_id,)).fetchone():
        raise ValueError("pick a subject set that exists")
    if conn.execute("select 1 from sections where name = %s and grad_year = %s",
                    (name, grad_year)).fetchone():
        raise ValueError(f"{section_label(name, grad_year)} already exists")
    conn.execute(
        "insert into sections (name, grad_year, subject_set_id) values (%s, %s, %s::uuid)",
        (name, grad_year, subject_set_id),
    )
    return section_label(name, grad_year)


def db_section(conn, section_id):
    """One section, opened: who is in it, the code that lets the next person
    in, the subjects it follows, and the template week it has so far."""
    head = conn.execute(
        "select s.name, s.grad_year, ss.name from sections s"
        " left join subject_sets ss on ss.id = s.subject_set_id where s.id = %s::uuid",
        (section_id,),
    ).fetchone()
    if not head:
        raise ValueError("no such section")
    members = [
        {"id": str(r[0]), "name": r[1], "roll_no": r[2], "status": r[3], "role": r[4]}
        for r in conn.execute(
            "select id, name, roll_no, status, role from profiles"
            " where section_id = %s::uuid order by name",
            (section_id,),
        )
    ]
    codes = [
        {"code": r[0], "name": r[1]}
        for r in conn.execute(
            "select s.code, s.name from subjects s"
            " join subject_set_members m on m.subject_code = s.code"
            " where m.set_id ="
            " (select subject_set_id from sections where id = %s::uuid)"
            " order by s.sort, s.code",
            (section_id,),
        )
    ]
    timetable = [
        {"day": r[0], "period": r[1], "code": r[2]}
        for r in conn.execute(
            "select day, period, subject_code from section_timetable"
            " where section_id = %s::uuid order by day, period",
            (section_id,),
        )
    ]
    # Who the registrar says is in this section and the app has never seen.
    #
    # roll_list is the institute's answer to "who belongs here" and profiles is
    # the app's answer to "who turned up"; the gap between them is the only
    # list that says who still has to be chased, and until now nothing could
    # show it -- a section screen could say 12 members and not that 93 people
    # were missing from it.
    #
    # Joined on the roll number rather than the scholar number because that is
    # the one both sides always hold: a Google joiner gets it copied out of
    # roll_list, and somebody who came through an invite typed it themselves.
    # Which is exactly why it is compared folded -- ' 26a026 ' and '26A026' are
    # one person, and a student who is listed as missing while sitting in the
    # members table above is worse than useless.
    #
    # Compared on the SEAT and not the string, which same_seat() decides and
    # 0052 explains: the registrar writes 26I060 and the people who joined by
    # invite a year before anybody had his file wrote I60. Matching the strings
    # would list this install's own admin as a student who never turned up,
    # which is the one row it is most obviously wrong about.
    missing = [
        {"scholar_no": r[0], "roll_no": r[1], "name": r[2]}
        for r in conn.execute(
            "select r.scholar_no, r.roll_no, r.name from roll_list r"
            " where r.section_id = %s::uuid"
            "   and not exists (select 1 from profiles p"
            "                    where same_seat(p.roll_no) = same_seat(r.roll_no))"
            " order by r.roll_no",
            (section_id,),
        )
    ]
    return {"id": str(section_id), "label": section_label(head[0], head[1]),
            "set": head[2], "members": members, "subjects": codes,
            "timetable": timetable, "missing": missing,
            "invite": db_invite(conn, section_id)}


# How long a minted code lasts and how many it lets in. The same numbers
# db_bootstrap uses for the very first one, because it is the same kind of
# thing: a code that opens a section for as long as an intake takes to arrive.
INVITE_DAYS = 30


def db_mint_invite(conn, section_id):
    """A fresh code for one section, because without one nobody can join it.

    0044 reads the section off the invite, so a section with no code is a
    section with no door -- and an admin cannot mint their own until there is
    an admin, which takes somebody joining first. This is that first code, and
    the super admin is the only one above the section line who can make it.
    """
    if not conn.execute("select 1 from sections where id = %s::uuid",
                        (section_id,)).fetchone():
        raise ValueError("no such section")
    code = secrets.token_hex(4)
    conn.execute(
        "insert into invites (code, expires_at, section_id) "
        f"values (%s, now() + interval '{INVITE_DAYS} days', %s::uuid)",
        (code, section_id),
    )
    return code


def db_reseed_section_weeks(conn, section_id):
    """Push a section's template onto every member's own week. Returns people.

    The trigger on profiles seeds a member once, when they join, and that used
    to be enough because a student could then fix their own week. Nobody can
    now -- so a template that did not reach the people already in the section
    would be a correction nobody ever sees, and the app would go on filing
    lectures under the subject the old grid named.

    Replaced, not merged, for the same reason the template is: a week half a
    correction old is the state nobody can look at and tell is wrong. Both
    statements name the section, so a missing where clause cannot reach past
    it -- this runs on the owning connection, where no policy would stop one.

    Every member, pending and blocked included, exactly as the trigger seeds
    them: status decides what somebody may do, never which week is theirs.
    """
    conn.execute("delete from timetable t using profiles p"
                 " where t.profile_id = p.id and p.section_id = %s::uuid",
                 (section_id,))
    conn.execute(
        "insert into timetable (profile_id, section_id, day, period, subject_code)"
        " select p.id, p.section_id, s.day, s.period, s.subject_code"
        "   from profiles p join section_timetable s on s.section_id = p.section_id"
        "  where p.section_id = %s::uuid",
        (section_id,),
    )
    return conn.execute("select count(*) from profiles where section_id = %s::uuid",
                        (section_id,)).fetchone()[0]


def db_set_section_timetable(conn, section_id, rows):
    """Replace one section's template week, all of it or none of it, and put
    it on every member of that section. Returns (periods, people).

    The rows have already been through parse_timetable, which is where a bad
    paste is refused; this only writes. Scoped to the one section in both
    statements -- the delete as much as the insert, since on this connection a
    missing where clause would take out every section's Monday.

    The re-seed is inside the same transaction: a template that landed while
    the weeks under it did not is the half-applied grid this whole path is
    built to refuse.
    """
    with conn.transaction():
        conn.execute("delete from section_timetable where section_id = %s::uuid",
                     (section_id,))
        for day, period, code in rows:
            conn.execute(
                "insert into section_timetable (day, period, subject_code, section_id)"
                " values (%s, %s, %s, %s::uuid)",
                (day, period, code, section_id),
            )
        people = db_reseed_section_weeks(conn, section_id)
    return len(rows), people


GATE_PAGE = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>recarve · MANIT first year</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600&amp;family=Kalam:wght@400;700&amp;display=swap">
<style>
:root{color-scheme:light dark;--bg:#faf9f4;--fg:#16183d;--mut:#5b6070;--line:#e6e4da;
  /* The same purple the app is built on, to the digit. These screens are the
     first thing anybody sees and the app is the second: a brand that changes
     colour between the login button and the first screen behind it reads as
     two products. Deep enough to carry white at 7.3:1 and to read on the
     paper at 7.1:1, which is what PAGE's own comment says about it. */
  --accent:#6534c9;--accent-fg:#fff;--err:#d1344b;
  /* Admin ink. Not a bolted-on red -- red is the error colour and already
     means something. This is the page's own ink, filled: the accent stays the
     ordinary blue action, grey stays neutral, and a solid slab of ink is what
     only an admin can press. The same pair marks the same thing inside the
     app, on the Me tab, so the treatment is one thing in two files. */
  --admin:#232733;--admin-fg:#faf9f4;--surface:#f3f2ea}
@media (prefers-color-scheme:dark){
  /* The accent goes pale in the dark, so what sits on it has to go dark too --
     white on it is 2.8:1. Same pair PAGE carries. */
  :root{--bg:#0f1115;--fg:#e7e9ee;--mut:#98a0ad;--line:#262a32;
    --accent:#ac93ff;--accent-fg:#0f1115;--err:#e5484d;
    /* Ink inverts with the paper: a near-black slab on a near-black ground is
       not a slab. Same job, same contrast, opposite end of the ramp. */
    --admin:#dfe4f0;--admin-fg:#0f1115;--surface:#171a20}}
*{box-sizing:border-box}
body{margin:0;min-height:100dvh;display:grid;place-items:center;padding:24px;background:var(--bg);
  color:var(--fg);font:16px/1.5 Figtree,-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  /* A name is whatever the joiner typed: one 80-character word with no spaces
     in it used to push this page sideways. */
  overflow-wrap:break-word}
main{width:100%;max-width:23rem}
h1{font-size:1.45rem;margin:0 0 .3rem;letter-spacing:-.01em}
p{color:var(--mut);margin:0 0 1.4rem}
label{display:block;font-size:.8rem;color:var(--mut);margin:0 0 .3rem}
/* --mut, not --line, for the same reason .row select takes it below: --line is
   1.24:1 on the ground and 1.4.11 wants 3:1 for a control's boundary. With
   background:transparent that border is the only thing saying a box is here. */
input{width:100%;padding:.7rem .8rem;margin:0 0 .9rem;font:inherit;border:1px solid var(--mut);
  border-radius:10px;background:transparent;color:var(--fg)}
input:focus{outline:2px solid var(--accent);outline-offset:-1px;border-color:transparent}
button{min-height:44px;width:100%;font:inherit;font-weight:600;line-height:1;border:0;
  border-radius:10px;background:var(--accent);color:var(--accent-fg)}
.err{color:var(--err);font-size:.88rem;min-height:1.2em;margin:.7rem 0 0}
.row{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:.75rem 0;
  border-bottom:1px solid var(--line)}
.row button{width:auto;padding:0 .9rem}
/* --line is 1.24:1 on the ground, which WCAG 1.4.11 wants at 3:1 for the
   boundary of a control -- and with no arrow drawn either, the only
   control on this screen read as static text. --mut is 5.24:1. */
.row select{width:auto;min-height:44px;font:inherit;font-size:1rem;color:var(--fg);
  background:transparent;border:1px solid var(--mut);border-radius:10px;padding:0 .5rem}
/* body's overflow-wrap does not shrink a flex item's automatic minimum
   size, so a 40-character name with no spaces in it still pushed this row --
   and the page with it -- sideways. Same job .row .name does in PAGE. */
.row b{overflow-wrap:anywhere}
.row small{display:block;color:var(--mut);font-size:.8rem}
/* The one link on these screens, and on /admin the only way back out.
   An anchor inherits body's 16px/1.5 and gets a ~19px box; nothing else here
   is under 44. */
main>p>a{display:inline-flex;align-items:center;min-height:44px}
h2{font-size:1rem;margin:2rem 0 .2rem}
.by{color:var(--fg);margin:0 0 .35rem}
/* The section is shown, not asked: there is one, and a box you can type in
   invites somebody to type the wrong one. Still an input so it sits in the
   same column as the fields around it, greyed the way its label is. */
input[readonly]{color:var(--mut)}

/* ---- The admin panel. Every action on this screen is one only an admin may
   take, so all of them are inked and the accent is left to navigation. */
.tag{display:inline-block;vertical-align:middle;margin-left:.45rem;padding:.1rem .45rem;
  border-radius:6px;background:var(--admin);color:var(--admin-fg);
  font-size:.72rem;font-weight:650;letter-spacing:.005em}
button.adm{background:var(--admin);color:var(--admin-fg)}
/* The quieter half of the same ink, for the reversible no: a ring rather than
   a slab, so Reject does not shout as loudly as Approve. Its own text is the
   contrast that matters, and the ring is 14:1 on the ground either way. */
button.ghost{background:transparent;color:var(--admin);
  box-shadow:inset 0 0 0 1px var(--admin)}
/* Below .adm and .ghost so it wins over both -- same specificity, later rule.
   Not opacity: the JS swaps the label for "Joining…"/"Logging in…"/"Saving…"
   when it disables the button, so the one moment the word carries information
   is the one moment a 50% fade made it 2.24:1 -- near-white on pale blue, in
   daylight, on the phone this form is actually filled in on. Greying it says
   "not pressable" just as plainly and keeps the word readable: --mut on --bg
   is 5.23:1 light and 7.17:1 dark, the ratio .hint and the input borders
   already carry. */
button[disabled]{background:var(--mut);color:var(--bg);box-shadow:none}
.counts{display:flex;gap:.5rem;margin:0 0 1.3rem}
.counts div{flex:1;min-width:0;padding:.55rem .6rem;border:1px solid var(--line);
  border-radius:10px}
.counts b{display:block;font-size:1.45rem;font-weight:700;line-height:1.15;
  font-variant-numeric:tabular-nums}
.counts span{font-size:.8rem;color:var(--mut)}
/* A queue with somebody in it is the whole reason this screen gets opened. */
.counts .hot{border-color:var(--admin)}
/* Three controls now on the widest row -- a role picker, Block and Reset
   password -- and 390px is the width that has to hold them. flex:none would
   rather push the page sideways than wrap, and a members list that scrolls
   sideways is a members list with a control off the edge of it. */
.acts{display:flex;align-items:center;gap:.4rem;flex:0 1 auto;flex-wrap:wrap;
  justify-content:flex-end}
.acts button{width:auto;padding:0 .75rem}
.invite{font-size:1.45rem;font-weight:700;letter-spacing:.05em;
  font-variant-numeric:tabular-nums}
.none{color:var(--mut);font-size:.88rem;margin:.5rem 0 0;overflow-wrap:anywhere}
/* What a password has to be, said above the box rather than after a refusal:
   a rule you learn from an error message is a rule you learn twice. --mut is
   5.24:1 on the ground in both themes. */
.hint{color:var(--mut);font-size:.8rem;margin:0 0 .8rem}
/* ---- The landing, which only the join screen carries. A cold tap off a
   WhatsApp link is the one moment this app has to sell itself, so it drops
   the plain card every other gate screen uses and commits to one look.
   Everything is scoped under .land -- login, waiting and the admin panel
   never see a line of it, which matters because this block shares a <style>
   with all three.

   The room: white, wide margins, one sentence at a time, and a 16:9 frame
   under each claim for the thing being claimed. The frames are empty in the
   markup below and say what belongs in them; drop an <img> or <video> in and
   the caption gets out of the way on its own.

   The one texture on the page is the ruled paper, and it is spent in exactly
   one place -- the block where the professor's sentence turns into notes.
   Handwriting on paper above, type below. That is the product, and it is the
   only thing here allowed to be loud.

   Two webfonts, loaded in the head with swap, so a slow font costs a reflow
   rather than a blank page. The login screens and the app use Figtree too. */
body:has(.land){display:block;padding:0;background:#faf9f4}
main:has(.land){max-width:none}
.land{--ink:#16183d; --body:#5b6070; --hair:#e6e4da;
  --paper:#faf9f4; --rule:#cbd9e6; --red:#c13a32;
  --bg:var(--paper); --fg:var(--ink); --mut:var(--body); --line:var(--hair);
  --accent:var(--ink); --accent-fg:var(--paper); --err:#b3261e;
  --surface:#f3f2ea;
  color-scheme:light;background:var(--paper);color:var(--ink);
  font:400 17px/1.6 Figtree,ui-sans-serif,system-ui,-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased}
.land .wrap{max-width:1080px;margin:0 auto;padding:0 24px 96px}
.land p{color:var(--body);margin:0 0 20px}
.land h1,.land h2,.land h3{color:var(--ink);font-weight:500;
  letter-spacing:-.02em;margin:0}
/* The handwriting is spent twice on this page: the name at the top, and the
   sentence the professor said. Set at 3rem across every section heading it
   stopped being a voice and became a wallpaper. */
.land a{color:var(--ink)}

.land nav{display:flex;align-items:center;justify-content:space-between;
  padding:22px 0}
.land .logo{font-family:Kalam,"Segoe Print",cursive;font-size:1.45rem;
  font-weight:700;color:var(--ink)}
.land nav a{font-size:.95rem;color:var(--body);text-decoration:none;
  padding:10px 4px;display:inline-flex;align-items:center;min-height:44px}
.land nav a:hover{color:var(--ink)}

/* The hero: centred, one claim, one button, and the room to read it. */
.land .hero{text-align:center;padding:clamp(64px,13vh,150px) 0 0}
.land .hero h1{font-size:clamp(2.5rem,6.6vw,5.5rem);line-height:1.02;
  letter-spacing:-.035em;max-width:16ch;margin:0 auto}
/* The two sentences are the two halves of the joke; letting them reflow into
   each other loses it. */
.land .hero h1 span{display:block}
.land .hero .lede{font-size:clamp(1.1rem,1.9vw,1.5rem);line-height:1.5;
  margin:32px auto 0;max-width:36rem}
.land .hero .where{font-size:1rem;margin:20px auto 0;max-width:32rem}
.land .ctas{display:flex;flex-direction:column;align-items:center;gap:14px;
  margin:48px 0 0}
.land .cta{display:inline-flex;align-items:center;justify-content:center;
  gap:10px;min-height:54px;padding:0 26px;border-radius:12px;font-size:1.02rem;
  font-weight:600;text-decoration:none;background:var(--ink);color:#fff;
  border:0}
.land .cta:hover{background:#23265a}
.land .cta svg{flex:none}
/* The Google button is the front door now, so on this page it is the primary
   control rather than the alternative to one: the same markup /login carries,
   wearing the same clothes as every other button here. */
.land .gbtn{display:inline-flex;align-items:center;justify-content:center;
  width:auto;margin:0;padding:0 26px;min-height:54px;border-radius:12px;
  background:var(--ink);color:#fff;border:0;font-size:1.02rem;font-weight:600;
  gap:12px;text-decoration:none;box-sizing:border-box;white-space:nowrap}
.land .gbtn:hover{background:#23265a}
.land .gbtn svg{background:#fff;border-radius:50%;padding:3px;box-sizing:content-box}
.land .gor{display:none}

/* The frames. 16:9, because that is what a screen recording of this app is,
   and everything inside is clipped to the same corner. Until something is in
   one it holds its caption instead of collapsing -- an empty frame that says
   what it is for is a note to the person filling it, not a broken image. */
.land .shot{margin:64px 0 0;padding:0;position:relative;aspect-ratio:16/9;
  border-radius:26px;overflow:hidden;border:0;display:grid;place-items:center;
  background:
    radial-gradient(70% 90% at 12% 8%,rgba(122,92,255,.30),transparent 62%),
    radial-gradient(60% 80% at 88% 18%,rgba(32,196,168,.22),transparent 60%),
    radial-gradient(70% 70% at 70% 100%,rgba(255,138,76,.16),transparent 62%),
    #0b0d13;
  box-shadow:0 40px 80px -48px rgba(22,24,61,.55)}
.land .shot>img,.land .shot>video{width:100%;height:100%;object-fit:cover;
  display:block}
.land .shot figcaption{font-size:.9rem;color:rgba(255,255,255,.62);
  text-align:center;padding:0 24px;max-width:36ch}
/* The caption is the empty state. A frame holding anything -- the stand-in
   film or the real thing -- does not need to be told what it is for. */
.land .shot:has(img) figcaption,.land .shot:has(video) figcaption,
.land .shot:has(>div) figcaption{display:none}
/* A film dropped in over an animation wins the frame. */
.land .shot:has(img)>div,.land .shot:has(video)>div{display:none}
.land .hero-shot{margin-top:64px}

/* One section heading, centred, the way the page opens. */
.land .sect{padding:clamp(88px,14vh,176px) 0 0}
.land .sect>h2{font-size:clamp(1.9rem,3.6vw,3rem);line-height:1.12;
  letter-spacing:-.03em;text-align:center;max-width:20ch;margin:0 auto}
.land .sect>h2+p{text-align:center;max-width:36rem;margin:20px auto 0;
  font-size:1.08rem}

/* A claim and the thing it claims, side by side on a wide screen and stacked
   on a phone. The text column stays narrow at every width: a line of body
   copy 90 characters long is not read, it is skimmed. */
.land .row{display:grid;gap:20px 72px;align-items:center;padding:72px 0 0}
@media (min-width:900px){
  .land .row{grid-template-columns:minmax(0,4fr) minmax(0,7fr);padding:72px 0 0}
  .land .row .shot{margin:0}
  /* The frame keeps the wide column whichever side it is on: ordering alone
     would have swapped the contents and left the picture in the narrow one. */
  .land .row.flip{grid-template-columns:minmax(0,7fr) minmax(0,4fr)}
  .land .row.flip>div{order:2}}
.land .row h3{font-size:1.6rem;line-height:1.2;letter-spacing:-.02em;
  margin:0 0 14px}
.land .row p{margin:0;max-width:32ch;font-size:1.05rem;line-height:1.6}

/* The proof, and the only ruled paper on the page. What was said is
   handwritten; what came back is typed; they sit on the same sheet. */
.land .demo{margin:56px 0 0;border-radius:24px;border:1px solid var(--hair);
  background:var(--paper);padding:34px 28px;overflow:hidden;
  background-image:repeating-linear-gradient(to bottom,
    transparent 0,transparent 31px,var(--rule) 31px,var(--rule) 32px);
  background-position:0 10px}
@media (min-width:760px){.land .demo{padding:44px 56px}}
.land .demo h3{font-size:.92rem;font-weight:400;color:#7d8296;line-height:32px;
  margin:0}
.land .said{font-family:Kalam,"Segoe Print",cursive;color:var(--ink);
  font-size:clamp(1.3rem,3.4vw,1.75rem);line-height:64px;margin:0 0 32px}
.land .said em{font-style:normal}
.land .demo ul{margin:0 0 32px;padding-left:1.2rem;color:var(--ink)}
.land .demo li{line-height:32px}
.land .demo code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:.92em;overflow-wrap:anywhere}
.land .demo .q{color:#5b6070;line-height:32px;margin:0}
.land .demo .q b{color:var(--ink);font-weight:600}
.land .demo .q i{font-style:normal}
/* The red tick in the margin: the one pen mark on the page, where the sheet
   turns from what was said into what came of it. */
.land .turn{display:flex;align-items:center;gap:12px;color:var(--red);
  font-size:.92rem;line-height:32px;margin:0 0 32px}
.land .turn::after{content:"";flex:1;height:1px;background:currentColor;
  opacity:.3}

/* Three short ones, no frames -- not everything needs a picture. */
.land .three{display:grid;gap:36px;padding:64px 0 0}
@media (min-width:760px){.land .three{grid-template-columns:repeat(3,1fr);
  gap:44px}}
.land .three h3{font-size:1.1rem;margin:0 0 8px}
.land .three p{margin:0;font-size:.98rem}

/* The steps are genuinely one after another, so they are numbered. */
.land .steps{display:grid;gap:36px;padding:56px 0 0;margin:0;list-style:none;
  counter-reset:s}
@media (min-width:760px){.land .steps{grid-template-columns:repeat(3,1fr);
  gap:44px}}
.land .steps li{padding:20px 0 0;border-top:2px solid var(--ink)}
.land .steps li::before{counter-increment:s;content:counter(s);display:block;
  font-size:.85rem;color:var(--body);margin:0 0 8px;
  font-variant-numeric:tabular-nums}
.land .steps b{display:block;font-size:1.1rem;font-weight:500;margin:0 0 6px}
.land .steps span{color:var(--body);font-size:.98rem}

/* The last door. The button again, and under it the invite form for anybody
   the registrar's list has not caught up with. */
.land .end{text-align:center;padding:clamp(96px,16vh,190px) 0 0}
.land .end h2{font-size:clamp(1.9rem,3.6vw,3rem);line-height:1.12;
  letter-spacing:-.03em;max-width:20ch;margin:0 auto}
.land .end>p{max-width:34rem;margin:20px auto 0}
.land footer{text-align:center;color:var(--body);font-size:.9rem;
  padding:96px 0 0}
.land footer p{margin:0}
.land footer p+p{margin:10px 0 0;display:flex;gap:22px;justify-content:center}
.land footer a{color:var(--body);text-underline-offset:3px;
  display:inline-flex;align-items:center;min-height:44px}
.land footer a:hover{color:var(--ink)}

/* ---- Motion. Two kinds, and no library: the hero arriving once on load,
   and the frames arriving as they are scrolled to. The second is a
   scroll-driven animation -- the browser ties the keyframes to how far the
   element has come up the viewport, which is the scroll-linked feel a motion
   library is usually imported for, at no bytes and on the compositor rather
   than on the main thread of a phone.

   It is inside @supports on purpose. Where the timeline is not understood the
   rule never applies, so the page is simply the page with nothing hidden --
   which is the failure a reveal effect has to have, because the other one is
   a blank page. */
@keyframes lift{from{opacity:0;transform:translateY(10px)}to{opacity:1;
  transform:none}}
.land .hero>*{animation:lift .7s cubic-bezier(.2,.7,.2,1) both}
.land .hero .lede{animation-delay:.08s}
.land .hero .where{animation-delay:.12s}
.land .hero .ctas{animation-delay:.16s}
@supports (animation-timeline:view()){
  @media (prefers-reduced-motion:no-preference){
    /* Only the frames and the sheet: the words stay where they were put.
       A page where every paragraph slides in is a page you wait for. */
    .land .row .shot,.land .demo{animation:lift linear both;
      animation-timeline:view();animation-range:entry 8% cover 26%}}}

@media (prefers-reduced-motion:reduce){
  .land *,.land *::before{animation:none!important}}
/* ---- The four films in the frames: .hr the hero, .mx the mixed line, .qz
   the quiz, .at attendance. Each is scoped to its own prefix, sized in cqi
   off its own frame, and goes when a real recording replaces its markup. */
.hr{position:relative;width:100%;aspect-ratio:16/9;container-type:inline-size;overflow:hidden;font-family:Figtree,system-ui,sans-serif;color:#e7e9ee;-webkit-font-smoothing:antialiased}
.hr *{box-sizing:border-box;margin:0;padding:0}
.hr-stage{position:absolute;left:50%;top:50%;width:78cqi;transform:translate(-50%,-50%)}
.hr-bar{position:relative;display:flex;align-items:center;gap:1.6cqi;padding:1.5cqi 2.2cqi 1.5cqi 4.6cqi;background:#171a20;border:1px solid rgba(255,255,255,.08);border-radius:1.36cqi;font-size:2.6cqi;line-height:1.2;animation:hr-fade 10s infinite}
.hr-dot{position:absolute;left:2.2cqi;top:50%;width:1.3cqi;height:1.3cqi;margin-top:-.65cqi;border-radius:50%}
.hr-dot-rec{opacity:0;animation:hr-recvis 10s infinite}
.hr-dot-rec::before{content:"";position:absolute;inset:0;border-radius:50%;background:#e5484d;animation:hr-pulse 1.4s ease-in-out infinite}
.hr-dot-ok{background:#ac93ff;animation:hr-okvis 10s infinite}
.hr-status{display:grid;font-weight:600;white-space:nowrap}
.hr-status>span{grid-area:1/1}
.hr-s1{opacity:0;animation:hr-s1 10s infinite}
.hr-s2{opacity:0;animation:hr-s2 10s infinite}
.hr-s3{animation:hr-s3 10s infinite}
.hr-course,.hr-time{color:#98a0ad;white-space:nowrap}
.hr-time{font-variant-numeric:tabular-nums}
.hr-wave{flex:1;display:flex;align-items:center;justify-content:center;gap:.5cqi;height:3.2cqi;opacity:.45;transform:scaleY(.14);animation:hr-flat 10s infinite}
.hr-wave>span{width:.5cqi;height:100%;border-radius:1cqi;background:#e7e9ee;opacity:.75;transform:scaleY(var(--h));animation:hr-wave .9s ease-in-out infinite alternate}
.hr-wave>span:nth-child(3n){animation-duration:.7s}
.hr-wave>span:nth-child(4n+1){animation-duration:1.15s}
.hr-prog{height:.45cqi;margin:2.2cqi .8cqi 0;border-radius:1cqi;background:rgba(255,255,255,.08);overflow:hidden;animation:hr-fade 10s infinite}
.hr-fill{display:block;height:100%;background:#ac93ff;transform-origin:left;animation:hr-fill 10s ease-in-out infinite}
.hr-later{margin:.7cqi .8cqi 1.2cqi;text-align:right;font-family:Kalam,cursive;font-size:2.8cqi;line-height:1.2;color:#ac93ff;animation:hr-later 10s infinite}
.hr-card{padding:3cqi 3.4cqi 3.4cqi;background:#171a20;border:1px solid rgba(255,255,255,.08);border-radius:1.36cqi;animation:hr-card 10s infinite}
.hr-meta{font-size:2.4cqi;line-height:1.3;color:#98a0ad;margin-bottom:1.2cqi}
.hr-line{position:relative;width:fit-content;max-width:100%}
.hr-h{font-size:4cqi;font-weight:700;line-height:1.25;letter-spacing:-.01em;margin-bottom:1.8cqi}
.hr-li{font-size:3.1cqi;line-height:1.4;padding-left:1.2em}
.hr-li+.hr-li{margin-top:1cqi}
.hr-li::before{content:"";position:absolute;left:.3em;top:.55em;width:.34em;height:.34em;border-radius:50%;background:#ac93ff}
.hr-sup{position:relative;top:-.5em;font-size:.62em;line-height:0}
.hr-cover{position:absolute;inset:-.1em -.2em;background:#171a20;transform-origin:right;transform:scaleX(0);animation-duration:10s;animation-iteration-count:infinite;animation-timing-function:linear}
.hr-c1{animation-name:hr-c1}.hr-c2{animation-name:hr-c2}.hr-c3{animation-name:hr-c3}
@keyframes hr-fade{0%{opacity:0}3%,92%{opacity:1}98%,100%{opacity:0}}
@keyframes hr-pulse{0%,100%{opacity:1}50%{opacity:.35}}
@keyframes hr-recvis{0%,34%{opacity:1}38%,100%{opacity:0}}
@keyframes hr-okvis{0%,35%{opacity:0}39%,100%{opacity:1}}
@keyframes hr-s1{0%,34%{opacity:1}37%,100%{opacity:0}}
@keyframes hr-s2{0%,35%{opacity:0}38%,73%{opacity:1}76%,100%{opacity:0}}
@keyframes hr-s3{0%,74%{opacity:0}77%,100%{opacity:1}}
@keyframes hr-flat{0%,33%{transform:scaleY(1);opacity:1}39%,100%{transform:scaleY(.14);opacity:.45}}
@keyframes hr-wave{from{transform:scaleY(.22)}to{transform:scaleY(1)}}
@keyframes hr-fill{0%,38%{transform:scaleX(0)}50%,100%{transform:scaleX(1)}}
@keyframes hr-later{0%,44%{opacity:0;transform:translateY(.4em)}49%,92%{opacity:1;transform:none}98%,100%{opacity:0;transform:none}}
@keyframes hr-card{0%,49%{opacity:0;transform:translateY(1.5cqi)}53%,92%{opacity:1;transform:none}98%,100%{opacity:0;transform:none}}
@keyframes hr-c1{0%,53%{transform:scaleX(1)}59%,100%{transform:scaleX(0)}}
@keyframes hr-c2{0%,60%{transform:scaleX(1)}67%,100%{transform:scaleX(0)}}
@keyframes hr-c3{0%,68%{transform:scaleX(1)}75%,100%{transform:scaleX(0)}}
@media (prefers-reduced-motion:reduce){.hr *,.hr *::before{animation:none!important}}
.mx{position:relative;width:100%;aspect-ratio:16/9;container-type:inline-size;overflow:hidden;font-family:Figtree,"Noto Sans Devanagari","Nirmala UI","Kohinoor Devanagari",system-ui,sans-serif;color:#e7e9ee;-webkit-font-smoothing:antialiased}
.mx *{box-sizing:border-box;margin:0;padding:0}
.mx-stage{position:absolute;left:50%;top:50%;width:82cqi;transform:translate(-50%,-50%)}
.mx-card{padding:3cqi 3.6cqi 3.2cqi;background:#171a20;border:1px solid rgba(255,255,255,.08);border-radius:1.36cqi}
.mx-head{display:flex;align-items:center;gap:1.4cqi;font-size:2.6cqi;line-height:1.2;white-space:nowrap}
.mx-rec{position:relative;width:1.2cqi;height:1.2cqi;border-radius:50%;background:#e5484d;animation:mx-pulse 1.4s ease-in-out infinite}
.mx-live{font-weight:600}
.mx-course{margin-left:auto;color:#98a0ad}
.mx-wave{display:flex;align-items:center;gap:.45cqi;height:2.8cqi}
.mx-wave>span{width:.45cqi;height:100%;border-radius:1cqi;background:#e7e9ee;opacity:.6;transform:scaleY(var(--h));animation:mx-wave .9s ease-in-out infinite alternate}
.mx-wave>span:nth-child(3n){animation-duration:.7s}
.mx-wave>span:nth-child(4n+1){animation-duration:1.15s}
.mx-prev{margin-top:2.6cqi;font-size:3cqi;line-height:1.6;color:#98a0ad}
.mx-now{margin-top:.8cqi;font-size:4.3cqi;line-height:1.7;animation:mx-clear 9s infinite}
.mx-w{display:inline-block;isolation:isolate;animation-duration:9s;animation-iteration-count:infinite;animation-timing-function:ease-out}
.mx-en{position:relative;color:#ac93ff}
.mx-en::before{content:"";position:absolute;z-index:-1;inset:.16em -.2em .1em;border-radius:.3em;background:rgba(172,147,255,.16);transform-origin:left;animation-duration:9s;animation-iteration-count:infinite;animation-timing-function:ease-out}
.mx-w1{animation-name:mx-w1}
.mx-w2{animation-name:mx-w2}
.mx-w3{animation-name:mx-w3}
.mx-w4{animation-name:mx-w4}
.mx-w5{animation-name:mx-w5}
.mx-w5::before{animation-name:mx-h5}
.mx-w6{animation-name:mx-w6}
.mx-w7{animation-name:mx-w7}
.mx-w7::before{animation-name:mx-h7}
.mx-w8{animation-name:mx-w8}
.mx-w9{animation-name:mx-w9}
.mx-w10{animation-name:mx-w10}
.mx-w10::before{animation-name:mx-h10}
.mx-w11{animation-name:mx-w11}
.mx-foot{display:flex;align-items:center;justify-content:space-between;gap:2cqi;margin-top:2.2cqi}
.mx-chip{padding:.35em .9em;border:1px solid rgba(255,255,255,.08);border-radius:10cqi;font-size:2.3cqi;line-height:1.2;color:#98a0ad;white-space:nowrap}
.mx-note{font-family:Kalam,cursive;font-size:2.9cqi;line-height:1.2;color:#ac93ff;white-space:nowrap;animation:mx-note 9s infinite}
@keyframes mx-pulse{0%,100%{opacity:1}50%{opacity:.35}}
@keyframes mx-wave{from{transform:scaleY(.22)}to{transform:scaleY(1)}}
@keyframes mx-clear{0%,89%{opacity:1}95%,100%{opacity:0}}
@keyframes mx-note{0%,60%{opacity:0;transform:translateY(.3em)}65%,89%{opacity:1;transform:none}95%,100%{opacity:0;transform:none}}
@keyframes mx-w1{0%,5%{opacity:0;transform:translateY(.3em)}7.5%,100%{opacity:1;transform:none}}
@keyframes mx-w2{0%,9%{opacity:0;transform:translateY(.3em)}11.5%,100%{opacity:1;transform:none}}
@keyframes mx-w3{0%,13%{opacity:0;transform:translateY(.3em)}15.5%,100%{opacity:1;transform:none}}
@keyframes mx-w4{0%,17%{opacity:0;transform:translateY(.3em)}19.5%,100%{opacity:1;transform:none}}
@keyframes mx-w5{0%,21%{opacity:0;transform:translateY(.3em)}23.5%,100%{opacity:1;transform:none}}
@keyframes mx-h5{0%,22.5%{opacity:0;transform:scaleX(0)}27%,100%{opacity:1;transform:none}}
@keyframes mx-w6{0%,27%{opacity:0;transform:translateY(.3em)}29.5%,100%{opacity:1;transform:none}}
@keyframes mx-w7{0%,30%{opacity:0;transform:translateY(.3em)}32.5%,100%{opacity:1;transform:none}}
@keyframes mx-h7{0%,31.5%{opacity:0;transform:scaleX(0)}36%,100%{opacity:1;transform:none}}
@keyframes mx-w8{0%,36%{opacity:0;transform:translateY(.3em)}38.5%,100%{opacity:1;transform:none}}
@keyframes mx-w9{0%,44%{opacity:0;transform:translateY(.3em)}46.5%,100%{opacity:1;transform:none}}
@keyframes mx-w10{0%,48%{opacity:0;transform:translateY(.3em)}50.5%,100%{opacity:1;transform:none}}
@keyframes mx-h10{0%,49.5%{opacity:0;transform:scaleX(0)}54%,100%{opacity:1;transform:none}}
@keyframes mx-w11{0%,55%{opacity:0;transform:translateY(.3em)}57.5%,100%{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){.mx *,.mx *::before{animation:none!important}}
.qz{position:relative;width:100%;aspect-ratio:16/9;container-type:inline-size;overflow:hidden;font-family:Figtree,system-ui,sans-serif;color:#e7e9ee;-webkit-font-smoothing:antialiased}
.qz *{box-sizing:border-box;margin:0;padding:0}
.qz-stage{position:absolute;left:50%;top:50%;width:66cqi;transform:translate(-50%,-50%)}
.qz-card{padding:3.2cqi 3.6cqi 3.4cqi;background:#171a20;border:1px solid rgba(255,255,255,.08);border-radius:1.36cqi;animation:qz-card 8s infinite}
.qz-head{display:flex;justify-content:space-between;gap:2cqi;font-size:2.4cqi;line-height:1.3;color:#98a0ad;white-space:nowrap}
.qz-q{margin-top:2cqi;font-size:4.6cqi;font-weight:700;line-height:1.25;letter-spacing:-.01em}
.qz-sup{position:relative;top:-.5em;font-size:.62em;line-height:0}
.qz-ans{display:grid;align-items:center;height:11cqi;margin-top:2.4cqi;padding:0 3cqi;border:1px dashed rgba(255,255,255,.14);border-radius:1cqi;overflow:hidden}
.qz-row{grid-area:1/1;display:flex;align-items:baseline;gap:2.4cqi;white-space:nowrap}
.qz-a{font-size:5.4cqi;font-weight:700;line-height:1.2}
.qz-why{font-size:2.6cqi;color:#98a0ad}
.qz-blur{filter:blur(1.1cqi);opacity:0;animation:qz-blur 8s infinite}
.qz-clear{animation:qz-clear 8s infinite}
.qz-foot{display:flex;align-items:center;justify-content:space-between;gap:2cqi;margin-top:2.6cqi}
.qz-hint{font-family:Kalam,cursive;font-size:2.8cqi;line-height:1.2;color:#98a0ad;white-space:nowrap}
.qz-btnwrap{position:relative;display:block}
.qz-btn{display:grid;animation:qz-press 8s infinite}
.qz-face{grid-area:1/1;padding:.6em 1.25em;border:1px solid #ac93ff;border-radius:10cqi;font-size:2.8cqi;font-weight:600;line-height:1.2;text-align:center;white-space:nowrap}
.qz-show{background:#ac93ff;color:#0b0d13;opacity:0;animation:qz-show 8s infinite}
.qz-hide{border-color:rgba(172,147,255,.45);color:#ac93ff;animation:qz-hide 8s infinite}
.qz-tap,.qz-ripple{position:absolute;left:50%;top:50%;border-radius:50%;pointer-events:none;opacity:0}
.qz-tap{width:4.4cqi;height:4.4cqi;margin:-2.2cqi 0 0 -2.2cqi;background:rgba(231,233,238,.2);border:1px solid rgba(231,233,238,.6);animation:qz-tap 8s ease-in-out infinite}
.qz-ripple{width:12cqi;height:12cqi;margin:-6cqi 0 0 -6cqi;background:rgba(172,147,255,.28);animation:qz-ripple 8s ease-out infinite}
@keyframes qz-card{0%{opacity:0;transform:translateY(1cqi)}5%,90%{opacity:1;transform:none}96%,100%{opacity:0;transform:none}}
@keyframes qz-tap{0%,12%{opacity:0;transform:translate(7cqi,5cqi)}24%{opacity:1;transform:none}27%{opacity:1;transform:scale(.78)}31%{opacity:1;transform:none}42%,100%{opacity:0;transform:translate(3cqi,4cqi)}}
@keyframes qz-ripple{0%,27%{opacity:0;transform:scale(.15)}28%{opacity:1;transform:scale(.2)}40%,100%{opacity:0;transform:none}}
@keyframes qz-press{0%,26%{transform:none}28%{transform:scale(.95)}31%,100%{transform:none}}
@keyframes qz-show{0%,29%{opacity:1}33%,100%{opacity:0}}
@keyframes qz-hide{0%,29%{opacity:0}33%,100%{opacity:1}}
@keyframes qz-blur{0%,29%{opacity:1}39%,100%{opacity:0}}
@keyframes qz-clear{0%,29%{opacity:0}39%,100%{opacity:1}}
@media (prefers-reduced-motion:reduce){.qz *{animation:none!important}}
.at{position:relative;width:100%;aspect-ratio:16/9;container-type:inline-size;overflow:hidden;font-family:Figtree,system-ui,sans-serif;color:#e7e9ee;-webkit-font-smoothing:antialiased}
.at *{box-sizing:border-box;margin:0;padding:0}
.at-stage{position:absolute;left:50%;top:50%;width:66cqi;transform:translate(-50%,-50%)}
.at-card{padding:3.2cqi 3.6cqi 3.4cqi;background:#171a20;border:1px solid rgba(255,255,255,.08);border-radius:1.36cqi;animation:at-card 9s infinite}
.at-head{display:flex;align-items:baseline;justify-content:space-between;gap:2cqi;white-space:nowrap}
.at-subj{font-size:3.6cqi;font-weight:700;line-height:1.2}
.at-code{margin-right:.6em;font-size:.66em;font-weight:500;color:#98a0ad}
.at-pct{font-size:4.4cqi;font-weight:700;line-height:1.2;font-variant-numeric:tabular-nums}
.at-roll{display:inline-block;vertical-align:baseline;clip-path:inset(0 -.2em)}
.at-col{position:relative;display:inline-block;transform:translateY(-100%)}
.at-next{position:absolute;left:0;top:100%}
.at-col-p{animation:at-rollp 9s cubic-bezier(.3,.7,.2,1) infinite}
.at-col-n{animation:at-rolln 9s cubic-bezier(.3,.7,.2,1) infinite}
.at-bar{position:relative;height:1cqi;margin-top:2.2cqi;border-radius:1cqi;background:rgba(255,255,255,.08)}
.at-fill{position:absolute;inset:0;border-radius:1cqi;background:#ac93ff;transform-origin:left;transform:scaleX(.82);animation:at-fill 9s ease-in-out infinite}
.at-min{position:absolute;left:75%;top:-.6cqi;bottom:-.6cqi;width:1px;background:rgba(231,233,238,.65)}
.at-minrow{position:relative;height:3.4cqi}
.at-minlabel{position:absolute;left:75%;top:.9cqi;transform:translateX(-50%);font-size:2.1cqi;line-height:1.2;color:#98a0ad;white-space:nowrap}
.at-big{margin-top:1cqi;font-size:3.4cqi;line-height:1.1;color:#98a0ad;white-space:nowrap}
.at-num{margin:0 .02em;font-size:3em;font-weight:700;line-height:1.1;color:#ac93ff;font-variant-numeric:tabular-nums}
.at-today{display:flex;align-items:center;justify-content:space-between;gap:2cqi;margin-top:2.6cqi;padding-top:2.6cqi;border-top:1px solid rgba(255,255,255,.08);white-space:nowrap}
.at-t{margin-right:.7em;font-size:2.9cqi;font-weight:600}
.at-muted{font-size:2.5cqi;color:#98a0ad}
.at-btns{display:flex;gap:1.2cqi}
.at-pwrap{position:relative;display:block}
.at-pbtn,.at-abtn{position:relative;display:grid;padding:.55em 1.1em;border:1px solid rgba(255,255,255,.12);border-radius:10cqi;font-size:2.6cqi;font-weight:600;line-height:1.2;text-align:center}
.at-abtn{color:#98a0ad}
.at-abtn,.at-pbtn{height:calc(1.2em + 1.1em + 2px)}
.at-pbtn{animation:at-press 9s infinite}
.at-pfill{position:absolute;inset:-1px;border-radius:10cqi;background:#ac93ff;animation:at-pfill 9s infinite}
.at-plab{position:relative;grid-area:1/1;display:flex;align-items:center;justify-content:center;height:1.2em}
.at-plab1{opacity:0;animation:at-plab1 9s infinite}
.at-plab2{color:#0b0d13;animation:at-plab2 9s infinite}
.at-check{display:block;flex:none;width:.85em;height:.85em;margin:-.12em .3em 0 0}
.at-tap,.at-ripple{position:absolute;left:50%;top:50%;border-radius:50%;pointer-events:none;opacity:0}
.at-tap{width:4.4cqi;height:4.4cqi;margin:-2.2cqi 0 0 -2.2cqi;background:rgba(231,233,238,.2);border:1px solid rgba(231,233,238,.6);animation:at-tap 9s ease-in-out infinite}
.at-ripple{width:11cqi;height:11cqi;margin:-5.5cqi 0 0 -5.5cqi;background:rgba(172,147,255,.28);animation:at-ripple 9s ease-out infinite}
@keyframes at-card{0%{opacity:0;transform:translateY(1cqi)}5%,89%{opacity:1;transform:none}95%,100%{opacity:0;transform:none}}
@keyframes at-tap{0%,12%{opacity:0;transform:translate(6cqi,5cqi)}24%{opacity:1;transform:none}27%{opacity:1;transform:scale(.78)}30%{opacity:1;transform:none}40%,100%{opacity:0;transform:translate(3cqi,4cqi)}}
@keyframes at-ripple{0%,27%{opacity:0;transform:scale(.15)}28%{opacity:1;transform:scale(.2)}39%,100%{opacity:0;transform:none}}
@keyframes at-press{0%,26%{transform:none}28%{transform:scale(.94)}31%,100%{transform:none}}
@keyframes at-pfill{0%,27%{opacity:0}32%,100%{opacity:1}}
@keyframes at-plab1{0%,27%{opacity:1}31%,100%{opacity:0}}
@keyframes at-plab2{0%,27%{opacity:0}31%,100%{opacity:1}}
@keyframes at-fill{0%,34%{transform:scaleX(.81)}42%,100%{transform:scaleX(.82)}}
@keyframes at-rollp{0%,35%{transform:none}41%,100%{transform:translateY(-100%)}}
@keyframes at-rolln{0%,41%{transform:none}49%,100%{transform:translateY(-100%)}}
@media (prefers-reduced-motion:reduce){.at *{animation:none!important}}
</style>
<main>__BODY__</main>
"""

JOIN_BODY = r"""<div class="land">
<div class="wrap">

<nav><span class="logo">recarve</span><a href="/login">Log in</a></nav>

<section class="hero">
  <h1>Sleep through class. <span>Wake up to notes.</span></h1>
  <p class="lede">Every lecture, written down. One person records the class. Everyone
  gets the notes: Hindi, English, or both in the same sentence.</p>
  <p class="where">Every first-year section at MANIT Bhopal, 2026&ndash;27.
  Your address says which one is yours.</p>
  <div class="ctas">__DOOR__</div>
</section>

<!-- THE FRAMES. Each <figure class="shot"> below is an empty 16:9 slot for a
     recording or animation. To fill one, put the media inside it as the first
     child and leave the caption where it is -- it hides itself once there is
     an <img> or <video> in the frame:

       <figure class="shot" id="shot-hero">
         <video src="/static/hero.mp4" autoplay muted loop playsinline></video>
         <figcaption>...</figcaption>
       </figure>

     Anything inside is cropped to fill, so cut to 16:9. A looping video wants
     muted + playsinline or iOS refuses to start it, and a poster= frame is
     what a phone on bad wifi sees first. -->
<figure class="shot hero-shot" id="shot-hero">
<div class="hr" aria-hidden="true">
  <div class="hr-stage">
    <div class="hr-bar">
      <span class="hr-dot hr-dot-rec"></span>
      <span class="hr-dot hr-dot-ok"></span>
      <span class="hr-status"><span class="hr-s1">Recording</span><span class="hr-s2">Making notes</span><span class="hr-s3">Notes ready</span></span>
      <span class="hr-course">MC1101 · Maths</span>
      <span class="hr-wave">
        <span style="--h:.35;animation-delay:-.2s"></span><span style="--h:.6;animation-delay:-.5s"></span><span style="--h:.9;animation-delay:-.1s"></span><span style="--h:.5;animation-delay:-.7s"></span><span style="--h:.75;animation-delay:-.3s"></span><span style="--h:1;animation-delay:-.6s"></span><span style="--h:.45;animation-delay:-.4s"></span><span style="--h:.8;animation-delay:-.8s"></span><span style="--h:.55;animation-delay:-.15s"></span><span style="--h:.95;animation-delay:-.45s"></span><span style="--h:.4;animation-delay:-.65s"></span><span style="--h:.7;animation-delay:-.25s"></span><span style="--h:.5;animation-delay:-.55s"></span><span style="--h:.3;animation-delay:-.35s"></span>
      </span>
      <span class="hr-time">12:04</span>
    </div>
    <div class="hr-prog"><span class="hr-fill"></span></div>
    <div class="hr-later">about 10 min later</div>
    <div class="hr-card">
      <div class="hr-meta">Today's notes</div>
      <div class="hr-line hr-h">Limits and derivatives<span class="hr-cover hr-c1"></span></div>
      <div class="hr-li hr-line">The limit is the basis of the derivative<span class="hr-cover hr-c2"></span></div>
      <div class="hr-li hr-line">Power rule: d/dx x<span class="hr-sup">n</span> = n·x<span class="hr-sup">n−1</span><span class="hr-cover hr-c3"></span></div>
    </div>
  </div>
</div>
  <figcaption>Hero animation: a lecture being recorded, and the notes
  appearing under it.</figcaption>
</figure>

<section class="sect">
  <h2>What ends up on the shelf</h2>
  <p>One place for the whole semester, filed by subject, on whatever phone you
  have.</p>

  <div class="row">
    <div>
      <h3>Hindi and English mixed</h3>
      <p>The way your professors actually talk, switching mid-sentence:
      transcribed properly, not garbled.</p>
    </div>
    <figure class="shot" id="shot-mixed">
<div class="mx" aria-hidden="true">
  <div class="mx-stage">
    <div class="mx-card">
      <div class="mx-head">
        <span class="mx-rec"></span>
        <span class="mx-live">Live transcript</span>
        <span class="mx-wave"><span style="--h:.4;animation-delay:-.2s"></span><span style="--h:.7;animation-delay:-.5s"></span><span style="--h:1;animation-delay:-.1s"></span><span style="--h:.55;animation-delay:-.7s"></span><span style="--h:.85;animation-delay:-.3s"></span><span style="--h:.45;animation-delay:-.6s"></span><span style="--h:.75;animation-delay:-.4s"></span><span style="--h:.35;animation-delay:-.8s"></span><span style="--h:.6;animation-delay:-.15s"></span><span style="--h:.9;animation-delay:-.45s"></span></span>
        <span class="mx-course">MC1101 · Maths</span>
      </div>
      <!-- divs, not paragraphs: `.land p` outranks `.mx *` and would repaint
           these in the page's body grey with the page's margins. -->
      <div class="mx-prev">अच्छा, आज से हम derivatives start करेंगे।</div>
      <div class="mx-now"><span class="mx-w mx-w1">तो</span> <span class="mx-w mx-w2">सबसे</span> <span class="mx-w mx-w3">पहले</span> <span class="mx-w mx-w4">हम</span> <span class="mx-w mx-w5 mx-en">limit</span> <span class="mx-w mx-w6">का</span> <span class="mx-w mx-w7 mx-en">concept</span> <span class="mx-w mx-w8">समझेंगे,</span> <span class="mx-w mx-w9">फिर</span> <span class="mx-w mx-w10 mx-en">power rule</span> <span class="mx-w mx-w11">देखेंगे</span></div>
      <div class="mx-foot">
        <span class="mx-chip">Hindi + English</span>
        <span class="mx-note">written exactly as spoken</span>
      </div>
    </div>
  </div>
</div>
    <figcaption>Preview: a mixed Hindi and English line being
      transcribed.</figcaption>
    </figure>
  </div>

  <div class="row flip">
    <div>
      <h3>Practice questions from every lecture</h3>
      <p>Answers hidden until you want them. Quiz one lecture, or the whole
      subject before the mid-sem.</p>
    </div>
    <figure class="shot" id="shot-quiz">
<div class="qz" aria-hidden="true">
  <div class="qz-stage">
    <div class="qz-card">
      <div class="qz-head"><span>MC1101 Maths practice</span><span>Question 3 of 12</span></div>
      <div class="qz-q">What is d/dx of x<span class="qz-sup">5</span>?</div>
      <div class="qz-ans">
        <div class="qz-row qz-blur"><span class="qz-a">5x<span class="qz-sup">4</span></span><span class="qz-why">power rule: 5·x<span class="qz-sup">5−1</span></span></div>
        <div class="qz-row qz-clear"><span class="qz-a">5x<span class="qz-sup">4</span></span><span class="qz-why">power rule: 5·x<span class="qz-sup">5−1</span></span></div>
      </div>
      <div class="qz-foot">
        <span class="qz-hint">try it in your head first</span>
        <span class="qz-btnwrap">
          <span class="qz-btn"><span class="qz-face qz-show">Show answer</span><span class="qz-face qz-hide">Hide answer</span></span>
          <span class="qz-ripple"></span>
          <span class="qz-tap"></span>
        </span>
      </div>
    </div>
  </div>
</div>
    <figcaption>Preview: a practice question, and the answer being
      revealed.</figcaption>
    </figure>
  </div>

  <div class="row">
    <div>
      <h3>Your 75%, per subject</h3>
      <p>Mark attendance in one tap. It tells you exactly how many classes you
      can miss, and never rounds you up.</p>
    </div>
    <figure class="shot" id="shot-attendance">
<div class="at" aria-hidden="true">
  <div class="at-stage">
    <div class="at-card">
      <div class="at-head">
        <div class="at-subj"><span class="at-code">MC1101</span>Maths</div>
        <div class="at-pct">8<span class="at-roll"><span class="at-col at-col-p">1<span class="at-next">2</span></span></span>%</div>
      </div>
      <div class="at-bar"><span class="at-fill"></span><span class="at-min"></span></div>
      <div class="at-minrow"><span class="at-minlabel">75% needed</span></div>
      <div class="at-big">You can miss <span class="at-roll at-num"><span class="at-col at-col-n">4<span class="at-next">5</span></span></span> more</div>
      <div class="at-today">
        <div class="at-when"><span class="at-t">Today</span><span class="at-muted">9:00 lecture</span></div>
        <div class="at-btns">
          <span class="at-pwrap">
            <span class="at-pbtn">
              <span class="at-pfill"></span>
              <span class="at-plab at-plab1">Present</span>
              <span class="at-plab at-plab2"><svg class="at-check" viewBox="0 0 12 12"><path d="M2.4 6.3l2.3 2.3 4.9-5.1" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>Present</span>
            </span>
            <span class="at-ripple"></span>
            <span class="at-tap"></span>
          </span>
          <span class="at-abtn"><span class="at-plab">Absent</span></span>
        </div>
      </div>
    </div>
  </div>
</div>
    <figcaption>Preview: marking a class, and the number of misses
      left moving.</figcaption>
    </figure>
  </div>

  <div class="three">
    <div><h3>Everything in one place, per subject</h3><p>Everyone&rsquo;s notes,
      slides and photos of the board, not scattered across five WhatsApp
      groups and Teams.</p></div>
    <div><h3>Somewhere to ask</h3><p>Stuck at 1&nbsp;am? Post a doubt under the
      lecture. Classmates answer, the best answer rises.</p></div>
    <div><h3>Readable with no signal</h3><p>Anything you&rsquo;ve opened stays
      readable in a dead corridor. Built for phones and bad wifi.</p></div>
  </div>
  <div class="three">
    <div><h3>Past papers</h3><p>Previous years&rsquo; mid-sems and end-sems,
      filed under the subject they belong to, the night before you need
      them.</p></div>
    <div><h3>A chat per subject</h3><p>One room for Maths, one for Physics.
      The question about tomorrow&rsquo;s tutorial goes where the people who
      know are.</p></div>
    <div><h3>The notice board</h3><p>Announcements from your CR, pinned at the
      top, not forty messages up in a group you muted.</p></div>
  </div>
</section>

<section class="sect">
  <h2>And the half that is not lectures</h2>
  <p>The same app carries the rest of first year, because the rest of first
  year is also scattered across eleven group chats.</p>
  <div class="three">
    <div><h3>Coming up on campus</h3><p>Every fest, talk, audition and deadline
      on one list, with the date it actually happens on.</p></div>
    <div><h3>Clubs and societies</h3><p>Who runs what, what they do, and where
      to find them when recruitment opens.</p></div>
    <div><h3>Finding your way</h3><p>The campus map. Which building the lab is
      in, where the department sits, what the canteen is called.</p></div>
  </div>
  <div class="three">
    <div><h3>The feed</h3><p>Your section, talking. Lost keys, a change of room,
      somebody selling a drafter.</p></div>
    <div><h3>Confessions</h3><p>Anonymous, and anonymous properly: no
      name is stored against it, so nobody can be shown one later.</p></div>
    <div><h3>Who has contributed</h3><p>Standings for the people who record and
      upload, because somebody has to and it should be seen.</p></div>
  </div>
</section>

<section class="sect">
  <h2>What the professor said, and what you get</h2>
  <p>Ten minutes after the class ends, on the same page.</p>
  <div class="demo">
    <h3>What the professor said, MC1101, Tuesday</h3>
    <p class="said">&ldquo;&#2340;&#2379; &#2360;&#2348;&#2360;&#2375;
    &#2346;&#2361;&#2354;&#2375; &#2361;&#2350; <em>limit</em> &#2325;&#2366;
    <em>concept</em> &#2360;&#2350;&#2333;&#2375;&#2306;&#2327;&#2375;,
    &#2347;&#2367;&#2352; <em>power rule</em>
    &#2342;&#2375;&#2326;&#2375;&#2306;&#2327;&#2375;&rdquo;</p>
    <p class="turn">What you get, ten minutes later</p>
    <ul>
      <li>The limit is the basis of the derivative</li>
      <li>Power rule: <code>d/dx x&#8319; = n&#183;x&#8319;&#8315;&#185;</code></li>
    </ul>
    <p class="q"><b>Then it asks you:</b> what is d/dx of x&#8309;?
    <i>The answer stays hidden until you have had a go.</i></p>
  </div>
</section>

<section class="sect">
  <h2>One phone in the room is enough</h2>
  <ol class="steps">
    <li><b>Someone records</b><span>Any trusted classmate hits record, or uploads
      the audio after.</span></li>
    <li><b>It gets written up</b><span>Transcribed, summarised, and turned into
      practice questions, about ten minutes later.</span></li>
    <li><b>Everyone reads</b><span>The whole section gets the notes, filed under
      the right subject, on any phone.</span></li>
  </ol>
</section>

<section class="end">
  <h2>Sign in with your institute email</h2>
  <p>Your section, your seat and your name all come off the registrar&rsquo;s
  list. There is nothing to fill in.</p>
  <div class="ctas">__DOOR__</div>
</section>

<footer><p>Made by Anirudh Sahu, first-year ECE at MANIT Bhopal, who kept
falling asleep in class.</p>
<p><a href="https://www.linkedin.com/in/anirudh-sahu-4b245327b/" rel="me noopener"
target="_blank">LinkedIn</a> <a href="https://www.instagram.com/anirudh_sahu_12/"
rel="me noopener" target="_blank">Instagram</a></p></footer>
</div>
</div>
"""



def join_body():
    """The landing, which is now one button.

    Nothing is substituted into it any more: the invite form is gone, so
    there is no code to reflect, no inviter to name and nothing on the page
    that a stranger's query string can reach. /join still answers a POST --
    the endpoint and its rules are untouched -- it simply has no form on this
    page pointing at it.
    """
    return JOIN_BODY.replace("__DOOR__", front_door())


def front_door():
    """The button at the top of the landing, and again at the bottom of it.

    Google is not the alternative to the form any more, it is the door. The
    registrar's list is loaded, so a verified institute address is enough on
    its own to say who somebody is and which section they sit in -- db_google
    reads the scholar number out of the address and roll_list answers the
    rest. Nothing to chase down a WhatsApp group for, no password to invent,
    and a section nobody typed.

    The form stays underneath for the one case the list cannot answer:
    somebody the registrar has not published yet. Without Google configured
    that is the only door there is, so the button points at the form rather
    than at a route that would 404.

    GOOGLE_BUTTON carries its own <style> for /login, which this page neither
    wants nor may paste twice -- .land dresses .gbtn itself -- so only the
    markup after the style block is taken.
    """
    if not google_client():
        return '<a class="cta" href="/login">Log in</a>'
    return GOOGLE_BUTTON.split("</style>\n", 1)[-1]


# The Google button, in both doors and rendered by with_google() so that an
# install without a client id shows neither it nor a dead link. The G is
# Google's own four-colour mark, inline because the page is one file and a
# second request for an 18px icon is a second thing that can fail on the
# institute wifi.
GOOGLE_BUTTON = r"""<style>
.gbtn{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;
  box-sizing:border-box;margin:16px 0 4px;padding:13px 16px;border-radius:12px;
  border:1px solid rgba(0,0,0,.12);background:#fff;color:#1f1f1f;
  font-weight:600;font-size:15px;text-decoration:none}
.gbtn:active{opacity:.86}
.gor{margin:12px 0 0;text-align:center;font-size:13px;color:var(--mut)}
.gor::before,.gor::after{content:"";display:inline-block;width:56px;height:1px;
  margin:0 10px;vertical-align:middle;background:currentColor;opacity:.35}
</style>
<a class="gbtn" href="/auth/google"><svg width="18" height="18" viewBox="0 0 48 48"
 aria-hidden="true"><path fill="#4285F4" d="M45 24c0-1.6-.1-2.7-.4-4H24v7.5h12c-.2 2-1.5 5-4.4 7l6.7 5.2C42.2 36 45 30.6 45 24z"/><path fill="#34A853" d="M24 46c5.9 0 10.9-2 14.5-5.3l-6.7-5.2c-1.9 1.3-4.4 2.2-7.8 2.2-6 0-11-4-12.8-9.4l-7 5.4C7.9 41 15.4 46 24 46z"/><path fill="#FBBC05" d="M11.2 28.3A13.6 13.6 0 0 1 10.5 24c0-1.5.3-3 .7-4.3l-7-5.4A22 22 0 0 0 2 24c0 3.5.9 6.9 2.3 9.7z"/><path fill="#EA4335" d="M24 10.2c3.3 0 6.2 1.2 8.5 3.3l6-6C34.9 4.1 29.9 2 24 2 15.4 2 7.9 7 4.3 14.3l7 5.4C13 14.2 18 10.2 24 10.2z"/></svg>Continue with your institute email</a>
<p class="gor">or</p>
"""


def with_google(body):
    """A door with the Google button in it, or the same door without.

    The placeholder is filled at render time rather than at import, because
    whether this install has Google is an environment fact and the module is
    imported by the tests, the CLI and the worker as well as the server.
    """
    return body.replace("__GOOGLE__", GOOGLE_BUTTON if google_client() else "")


LOGIN_BODY = r"""<h1>recarve</h1>
<p>Section I notes.</p>
__GOOGLE__
<form id="f">
  <label for="roll">Roll number</label>
  <input id="roll" required autocomplete="username" autocapitalize="characters"
         autocorrect="off" spellcheck="false" placeholder="I0">
  <label for="pw">Password</label>
  <input id="pw" required type="password" autocomplete="current-password">
  <button>Log in</button>
  <p class="err" id="err"></p>
</form>
<p class="hint">Had yours reset by the admin? Sign in with your roll number, then
pick a new one.</p>
<p><a href="/">New here? Join with an invite code</a></p>
<script>
const $ = i => document.getElementById(i);
// A sign-in that came back from Google refused is a redirect to here carrying
// the sentence, because there is nowhere else to put it: the callback is a
// navigation, not a fetch, and a bare 403 page is how a student decides the
// app is broken rather than that they used the wrong account.
const gerr = new URLSearchParams(location.search).get('e');
if (gerr) $('err').textContent = gerr;
$('f').onsubmit = async e => {
  e.preventDefault();
  $('err').textContent = '';
  // /login opens a database connection before it answers, exactly as /join
  // does. Left alone the button looks dead on mobile data and the second tap
  // races the first -- and two taps here spend two of five tries.
  const b = $('f').querySelector('button');
  b.disabled = true;
  b.textContent = 'Signing in\u2026';
  try {
    const res = await fetch('/login', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({roll_no: $('roll').value, password: $('pw').value})});
    const j = await res.json().catch(() => ({}));
    // Always '/', whatever came back. The gate there is the one thing that
    // decides between the library, the waiting screen and the one that asks
    // for a password; a page that routed itself would be a second opinion
    // about who is allowed where, and the wrong one the day they disagree.
    if (res.ok) return location.assign('/');
    $('err').textContent = j.error || 'could not sign in';
  } catch (e) { $('err').textContent = 'no connection to the server'; }
  b.disabled = false;
  b.textContent = 'Log in';
};
</script>
"""


SET_PASSWORD_BODY = r"""<h1>Pick a password</h1>
<p>One thing before the library opens. Until you set a password, anyone who
knows your roll number can sign in as you, and a roll number is on every list
in the institute.</p>
<form id="f">
  <label for="pw">New password</label>
  <p class="hint">At least __MIN__ characters, and not your roll number. Use
  something you do not use anywhere else.</p>
  <input id="pw" required type="password" autocomplete="new-password" minlength="__MIN__">
  <label for="pw2">Type it again</label>
  <input id="pw2" required type="password" autocomplete="new-password">
  <button>Save and open the library</button>
  <p class="err" id="err"></p>
</form>
<script>
const $ = i => document.getElementById(i);
$('f').onsubmit = async e => {
  e.preventDefault();
  $('err').textContent = '';
  // Caught here because only this screen has both boxes. The server sees one
  // password and cannot tell a typo from a choice -- and the cost of a typo is
  // an account whose owner is locked out of it a minute after setting it.
  if ($('pw').value !== $('pw2').value) {
    $('err').textContent = 'those two do not match';
    return;
  }
  const b = $('f').querySelector('button');
  b.disabled = true;
  b.textContent = 'Saving\u2026';
  try {
    const res = await fetch('/password', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: $('pw').value})});
    const j = await res.json().catch(() => ({}));
    if (res.ok) return location.assign('/');
    $('err').textContent = j.error || 'could not save that';
  } catch (e) { $('err').textContent = 'no connection to the server'; }
  b.disabled = false;
  b.textContent = 'Save and open the library';
};
</script>
""".replace("__MIN__", str(MIN_PASSWORD))


WAIT_BODY = r"""<h1>Almost in</h1>
<p>Your request is with the admin. This page lets you through the moment they
approve you. Leave it open; it rechecks itself.</p>
<script>setTimeout(() => location.reload(), 15000);</script>
"""

BLOCKED_BODY = """<h1>No access</h1>
<p>This account has been blocked. Talk to whoever runs the class library.</p>
"""

ADMIN_BODY = r"""<p><a href="/">&lsaquo; Back to recarve</a></p>
<h1>Class admin<span class="tag">Admin</span></h1>
<p>Who is in, what each of them may do, and how the next person gets in.</p>
<div class="counts" id="counts"></div>

<h2>Waiting to be let in</h2>
<div id="list">loading&hellip;</div>

<h2>Members</h2>
<p>Students read. Trusted members upload, record and use Explain, that one
spends money. Admins also let people in. Resetting a password puts somebody
back to signing in with their roll number until they pick a new one. You cannot
change your own row: an admin who demotes themselves leaves a class nobody can
approve anyone into.</p>
<div id="members">loading&hellip;</div>

<h2>Invite code</h2>
<div id="invite"></div>

<h2>Reported</h2>
<div id="reports"></div>

<script>
const $ = id => document.getElementById(id);

// The ladder, handed down from ROLES in notes.py rather than typed again here.
// This is the one screen that offers every role at once, and a dropdown that
// has quietly stopped offering the newest one is a role nobody can be given.
const ROLES = __ROLES__;

// Both timestamps are seconds on the server's clock -- the same clock the rows
// were stamped by -- so a handset a few minutes out cannot report a joiner who
// has not asked yet.
function ago(then, now) {
  const d = Math.max(0, (now || 0) - (then || 0));
  if (d < 90) return 'just now';
  if (d < 3600) return Math.round(d / 60) + ' min ago';
  if (d < 172800) return Math.round(d / 3600) + ' h ago';
  return Math.round(d / 86400) + ' days ago';
}

// Names, roll numbers, phone numbers and report reasons are all whatever
// somebody typed into a form, so every one of them goes in with textContent.
// This screen is the admin account's, and none of that is a place to run
// script from.
function who(name, sub) {
  const el = document.createElement('div');
  el.innerHTML = '<b></b><small></small>';
  el.querySelector('b').textContent = name;
  el.querySelector('small').textContent = sub;
  return el;
}

const dot = (...bits) => bits.filter(Boolean).join(' \u00b7 ');

async function post(path, payload) {
  try {
    const r = await fetch(path, {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      alert(e.error || 'the server refused that');
    }
  } catch (e) { alert('no connection to the server'); }
}

// Every button here does one thing and then re-reads the screen: the counts,
// the queue and the members list all move together after any of them.
function btn(label, cls, fn) {
  const b = document.createElement('button');
  b.className = cls;
  b.textContent = label;
  b.onclick = async () => { b.disabled = true; await fn(); load(); };
  return b;
}

function roleSelect(p) {
  const sel = document.createElement('select');
  sel.setAttribute('aria-label', 'Role for ' + p.name);
  for (const r of ROLES) {
    const o = document.createElement('option');
    o.value = r; o.textContent = r;
    if (r === p.role) o.selected = true;
    sel.appendChild(o);
  }
  sel.onchange = async () => {
    sel.disabled = true;
    await post('/role', {id: p.id, role: sel.value});
    load();
  };
  return sel;
}

function tiles(counts) {
  const box = $('counts');
  box.innerHTML = '';
  // Only the two that are a queue go hot: "members" being non-zero is not news.
  for (const [n, label, queue] of [[counts.pending, 'waiting', true],
                                   [counts.members, 'members', false],
                                   [counts.blocked, 'blocked', false],
                                   [counts.reports, 'reported', true]]) {
    const t = document.createElement('div');
    if (queue && n) t.className = 'hot';
    t.innerHTML = '<b></b><span></span>';
    t.querySelector('b').textContent = n;
    t.querySelector('span').textContent = label;
    box.appendChild(t);
  }
}

function note(el, text) {
  const p = document.createElement('p');
  p.className = 'none';
  p.textContent = text;
  el.appendChild(p);
  return p;
}

function renderInvite(code) {
  const box = $('invite');
  box.innerHTML = '';
  if (!code) {
    return note(box, 'No code is live. Nobody can join until there is one.');
  }
  // Built here, not on the server: this page is reached over the tunnel as
  // often as over the wifi, and only the browser knows which address the
  // person on the other end of WhatsApp has to be able to open.
  const link = location.origin + '/?code=' + encodeURIComponent(code);
  const row = document.createElement('div');
  row.className = 'row';
  const shown = document.createElement('span');
  shown.className = 'invite';
  shown.textContent = code;
  const acts = document.createElement('div');
  acts.className = 'acts';
  // Not btn(): that one re-reads the screen afterwards, which would wipe the
  // word that says the copy worked. Copying is the whole action.
  const copy = document.createElement('button');
  copy.className = 'adm';
  copy.textContent = 'Copy join link';
  copy.onclick = async () => {
    try { await navigator.clipboard.writeText(link); copy.textContent = 'Copied'; }
    catch (e) { copy.textContent = 'Copy failed - read the code out'; }
  };
  acts.appendChild(copy);
  row.append(shown, acts);
  box.appendChild(row);
  // The link in full, because a copy that silently failed looks exactly like
  // one that worked, and this is the fallback somebody can read down a phone.
  note(box, link);
}

function renderReports(reports, now) {
  const box = $('reports');
  box.innerHTML = '';
  if (!reports.length) return note(box, 'Nothing has been reported.');
  for (const r of reports) {
    const row = document.createElement('div');
    row.className = 'row';
    const sub = dot(r.subject, r.reason || 'no reason given',
                    'reported by ' + r.by, ago(r.at, now));
    if (!r.material_id || r.status === 'removed') {
      const gone = document.createElement('small');
      gone.textContent = r.material_id ? 'removed' : 'already gone';
      row.append(who(r.filename || 'a file that no longer exists', sub), gone);
    } else {
      row.append(who(r.filename, sub),
                 btn('Remove', 'adm', () => post('/remove', {id: r.material_id})));
    }
    box.appendChild(row);
  }
}

async function load() {
  const el = $('list'), mem = $('members');
  // 503 when Postgres is down, 403 for an admin another admin just demoted:
  // neither body carries `pending`, and reading it blanked the whole screen.
  let j = null;
  try { const r = await fetch('/pending'); if (r.ok) j = await r.json(); } catch (e) {}
  if (!j) {
    el.textContent = mem.textContent =
      'Could not load this - the library may be offline. Reload to retry.';
    return;
  }
  tiles(j.counts || {});
  el.innerHTML = '';
  if (!j.pending.length) el.textContent = 'Nobody waiting.';
  for (const p of j.pending) {
    const row = document.createElement('div');
    row.className = 'row';
    const acts = document.createElement('div');
    acts.className = 'acts';
    // Reject is a block, not a delete: it is reversible from the row below,
    // and deleting a person would take their uploads and votes with them.
    acts.append(btn('Approve', 'adm', () => post('/approve', {id: p.id})),
                btn('Reject', 'ghost', () => post('/block', {id: p.id, blocked: true})));
    row.append(who(p.name, dot(p.roll_no, p.phone, ago(p.asked, j.now))), acts);
    el.append(row);
  }
  mem.innerHTML = '';
  if (!(j.members || []).length) mem.textContent = 'Nobody is in yet.';
  for (const p of j.members || []) {
    const row = document.createElement('div');
    row.className = 'row';
    // The password is here because it is stored as typed, and this screen is
    // the only place that is any use: a classmate rings from a corridor and
    // the admin reads it back, rather than resetting an account they cannot
    // see into and then having to reach them twice.
    const sub = dot(p.roll_no, p.phone,
                    p.password ? 'password ' + p.password : 'no password set yet',
                    p.status === 'blocked' ? 'blocked' : '');
    // Your own row has no controls: dropping your own admin, or blocking
    // yourself, would leave nobody who can let the next person in. The server
    // refuses both, and the database refuses them after that.
    if (p.id === j.me) {
      const you = document.createElement('small');
      you.textContent = p.role + ' - you';
      row.append(who(p.name, sub), you);
    } else {
      const acts = document.createElement('div');
      acts.className = 'acts';
      const blocked = p.status === 'blocked';
      acts.append(roleSelect(p),
                  btn(blocked ? 'Unblock' : 'Block', 'ghost',
                      () => post('/block', {id: p.id, blocked: !blocked})));
      // Only where there is one to reset. On a row that has never set a
      // password the button would do nothing and say it had.
      if (p.password) acts.append(btn('Reset password', 'ghost',
                                      () => post('/reset', {id: p.id})));
      row.append(who(p.name, sub), acts);
    }
    mem.append(row);
  }
  renderInvite(j.invite);
  renderReports(j.reports || [], j.now);
}
load();
</script>
""".replace("__ROLES__", json.dumps(list(ROLES)))


# ---- /super, the screen. -------------------------------------------------
# Its own document rather than a body inside GATE_PAGE, for the reason the
# whole feature is separate: GATE_PAGE is what a student is served, and none of
# this is ever sent to anybody not holding /super's cookie. It also wants a
# different room -- a 23rem card is the shape of a login box, and this is a
# table of every section there is.
#
# Same paint as the app to the digit: one purple accent, the 11/13/16/20/26
# type scale, the 7/11/14/18 corners. An admin screen that looks like a
# different product is an admin screen you distrust. Desktop first, because it
# is opened on a desk -- and the two tables scroll inside their own boxes so a
# phone gets a narrow page rather than a broken one.
SUPER_PAGE = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow">
<title>recarve super admin</title>
<style>
:root{color-scheme:light dark;--bg:#faf9f4;--fg:#16183d;--mut:#5b6070;--line:#e6e4da;
  --accent:#6534c9;--accent-fg:#fff;--err:#d1344b;--ok:#1a7f4b;
  --admin:#232733;--admin-fg:#faf9f4;--surface:#f3f2ea}
@media (prefers-color-scheme:dark){
  :root{--bg:#0f1115;--fg:#e7e9ee;--mut:#98a0ad;--line:#262a32;
    --accent:#ac93ff;--accent-fg:#0f1115;--err:#e5484d;--ok:#3ee089;
    --admin:#dfe4f0;--admin-fg:#0f1115;--surface:#171a20}}
*{box-sizing:border-box}
body{margin:0;min-height:100dvh;background:var(--bg);color:var(--fg);
  font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  overflow-wrap:break-word}
main{max-width:60rem;margin:0 auto;padding:24px 16px 64px}
/* The login box is the one screen here that is a card in the middle of
   nothing, so it says so rather than inheriting the console's width. Keyed on
   the form being present, the way GATE_PAGE keys the landing off .land --
   there are two bodies in this one document and only one of them is a box. */
body:has(#f){display:grid;place-items:center;padding:24px}
body:has(#f) main{width:100%;max-width:23rem;padding:0}
h1{font-size:26px;font-weight:700;letter-spacing:-.01em;margin:0 0 4px}
h2{font-size:20px;font-weight:600;margin:32px 0 4px}
h3{font-size:16px;font-weight:600;margin:0 0 4px}
p{color:var(--mut);margin:0 0 16px}
p.lede{font-size:13px}
label{display:block;font-size:11px;font-weight:500;letter-spacing:.02em;
  text-transform:uppercase;color:var(--mut);margin:0 0 4px}
/* --mut and not --line: with a transparent ground the border is the only
   thing saying a box is here, and 1.4.11 wants 3:1 for that. */
input,select,textarea{width:100%;padding:8px 12px;font:inherit;font-size:16px;
  color:var(--fg);background:transparent;border:1px solid var(--mut);border-radius:11px}
textarea{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;
  min-height:11rem;resize:vertical}
input:focus,select:focus,textarea:focus,button:focus-visible{outline:2px solid var(--accent);
  outline-offset:-1px}
button{min-height:44px;padding:0 16px;font:inherit;font-size:16px;font-weight:600;
  line-height:1;border:0;border-radius:11px;background:var(--accent);color:var(--accent-fg)}
/* Every write on this screen is one only the super admin can make, so the ink
   carries them and the accent is left to the ordinary action -- the same
   division /admin makes inside the app. */
button.adm{background:var(--admin);color:var(--admin-fg)}
button.ghost{background:transparent;color:var(--admin);box-shadow:inset 0 0 0 1px var(--admin)}
button[disabled]{background:var(--mut);color:var(--bg);box-shadow:none}
.card{border:1px solid var(--line);border-radius:18px;padding:16px;margin:0 0 16px;
  background:var(--surface)}
.grid{display:grid;gap:12px;grid-template-columns:1fr;align-items:end}
@media (min-width:40rem){.grid{grid-template-columns:2fr 1fr 2fr auto}}
/* A table is the right shape for sections and members on a desk and the wrong
   one on a phone, so it scrolls inside its own box rather than pushing the
   page sideways. */
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:11px;font-weight:600;letter-spacing:.02em;
  text-transform:uppercase;color:var(--mut);padding:0 12px 8px 0;white-space:nowrap}
td{padding:8px 12px 8px 0;border-top:1px solid var(--line);vertical-align:middle}
td.name{font-size:16px;font-weight:600}
td small{display:block;font-size:11px;color:var(--mut);font-weight:400}
td select,td input{width:auto;min-height:44px;font-size:13px}
td button{min-height:44px;font-size:13px}
.num{font-variant-numeric:tabular-nums}
.code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:16px;
  font-weight:700;letter-spacing:.05em}
.pill{display:inline-block;padding:2px 8px;border-radius:7px;font-size:11px;font-weight:600;
  background:var(--admin);color:var(--admin-fg)}
.pill.q{background:transparent;color:var(--mut);box-shadow:inset 0 0 0 1px var(--mut)}
.row{display:flex;flex-wrap:wrap;align-items:center;gap:12px}
.spread{justify-content:space-between}
.err{color:var(--err);font-size:13px;margin:8px 0 0;min-height:1.2em}
.ok{color:var(--ok);font-size:13px;margin:8px 0 0}
/* Every bad line at once, in the order they were pasted. A list you scroll is
   the point: the alternative is learning one mistake per attempt. */
ul.errs{margin:8px 0 0;padding:0 0 0 20px;color:var(--err);font-size:13px}
ul.errs li{margin:0 0 4px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.mut{color:var(--mut);font-size:13px}
.hide{display:none}
@media (prefers-reduced-motion:no-preference){main{animation:rise .18s ease-out both}}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
</style>
<main>__BODY__</main>
"""


SUPER_LOGIN_BODY = r"""<h1>recarve</h1>
<p class="lede">Super admin. If you were not looking for this, you want
<a href="/">the app</a>.</p>
<form id="f">
  <label for="em">Email</label>
  <input id="em" type="email" autocomplete="username" required>
  <label for="pw">Password</label>
  <input id="pw" type="password" autocomplete="current-password" required>
  <p class="err" id="err" role="alert"></p>
  <button id="go" type="submit">Sign in</button>
</form>
<script>
const $ = id => document.getElementById(id);
$('f').onsubmit = async e => {
  e.preventDefault();
  const go = $('go'), was = go.textContent;
  go.disabled = true; go.textContent = 'Signing in…'; $('err').textContent = '';
  try {
    const r = await fetch('/super/login', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: $('em').value, password: $('pw').value})});
    if (r.ok) { location.href = '/super'; return; }
    const said = await r.json().catch(() => ({}));
    $('err').textContent = said.error || 'that did not work';
  } catch (x) { $('err').textContent = 'no connection to the server'; }
  go.disabled = false; go.textContent = was;
};
</script>
"""


SUPER_BODY = r"""<div class="row spread">
  <div><h1>Every section</h1>
  <p class="lede">Above the section line. Nothing on this screen is narrowed to
  one class, which is the whole reason it is not in the app.</p></div>
  <span class="pill">Super admin</span>
</div>

<div class="card">
  <h3>Start a section</h3>
  <p class="lede">A name, the year they graduate, and the curriculum they
  follow. Shown everywhere as <b>Section&nbsp;I&nbsp;&rsquo;30</b>.</p>
  <form id="new" class="grid">
    <div><label for="nm">Name</label><input id="nm" required placeholder="II"></div>
    <div><label for="yr">Graduates</label>
      <input id="yr" class="num" type="number" min="2000" max="2100" required
             placeholder="2030"></div>
    <div><label for="set">Subject set</label><select id="set"></select></div>
    <div><button class="adm" id="mk" type="submit">Create</button></div>
  </form>
  <p class="err" id="newerr" role="alert"></p>
</div>

<div class="card scroll">
  <table>
    <thead><tr><th>Section</th><th>Subjects</th><th class="num">Members</th>
      <th>Invite code</th><th></th></tr></thead>
    <tbody id="rows"><tr><td colspan="5" class="mut">loading&hellip;</td></tr></tbody>
  </table>
</div>

<div id="one" class="hide">
  <h2 id="onename"></h2>
  <p class="lede" id="onesub"></p>

  <div class="card scroll">
    <h3>Members</h3>
    <p class="lede">A role changed here goes down the same path the class
    admin&rsquo;s own screen uses, and lands on their next tap.</p>
    <table><thead><tr><th>Who</th><th>Roll</th><th>Status</th><th>Role</th>
      <th>Access</th></tr></thead>
      <tbody id="members"></tbody></table>
    <p class="err" id="roleerr" role="alert"></p>
  </div>

  <div class="card scroll">
    <h3 id="missinghead">Not joined yet</h3>
    <p class="lede">On the institute&rsquo;s roll list for this section, with no
    account here. Nobody to chase means the section is all in.</p>
    <table><thead><tr><th>Who</th><th>Roll</th><th>Scholar no.</th></tr></thead>
      <tbody id="missing"></tbody></table>
  </div>

  <div class="card">
    <h3>Timetable</h3>
    <p class="lede">Paste <code>day,period,subject_code</code>, one line each.
    Every line is read before any of them is written, if one is wrong,
    none of them is.</p>
    <textarea id="csv" spellcheck="false"
      placeholder="day,period,subject_code&#10;Monday,1,MC1101&#10;Monday,2,MA1101"></textarea>
    <p class="lede" id="known"></p>
    <button class="adm" id="wr" type="button">Check and write</button>
    <p class="err" id="tterr" role="alert"></p>
    <ul class="errs hide" id="ttlist"></ul>
    <p class="ok hide" id="ttok"></p>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
// The ladder and the weekdays, handed down from ROLES and DAYS in notes.py
// rather than typed again here. A dropdown that has quietly stopped offering
// the newest role is a role nobody can be given.
const ROLES = __ROLES__;
const DAYS = __DAYS__;
let open_id = null;

// Section names, member names and roll numbers are all whatever somebody typed
// into a form, so every one of them goes in through textContent.
function cell(row, text, cls) {
  const td = document.createElement('td');
  if (cls) td.className = cls;
  td.textContent = text === null || text === undefined ? '' : String(text);
  row.appendChild(td);
  return td;
}

async function ask(path, payload) {
  const r = await fetch(path, payload === undefined ? {} : {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw body;
  return body;
}

// One place where a control disables itself, does the thing, and says where it
// went wrong -- because every control on this screen wants exactly that.
async function run(control, fn, err) {
  const was = control.textContent;
  control.disabled = true;
  if (err) $(err).textContent = '';
  try { await fn(); }
  catch (e) {
    const said = (e && e.error) || 'the server refused that';
    if (err) $(err).textContent = said; else alert(said);
  }
  control.disabled = false; control.textContent = was;
}

function sectionRow(s) {
  const tr = document.createElement('tr');
  const name = cell(tr, s.label, 'name');
  const year = document.createElement('small');
  year.textContent = 'graduates ' + s.grad_year;
  name.appendChild(year);
  cell(tr, s.set || 'no set');
  cell(tr, s.members, 'num');
  const code = cell(tr, '');
  if (s.invite) {
    code.className = 'code';
    code.textContent = s.invite;
  } else {
    // A section with no code has no door: join_with_invite reads the section
    // off the invite, so until one exists nobody can join this one at all.
    const b = document.createElement('button');
    b.className = 'ghost';
    b.textContent = 'Mint a code';
    b.onclick = () => run(b, async () => { await ask('/super/invite', {section: s.id}); await load(); });
    code.appendChild(b);
  }
  const act = cell(tr, '');
  const open = document.createElement('button');
  open.textContent = 'Open';
  open.onclick = () => show(s.id).catch(e => alert(e.error || 'could not open that'));
  act.appendChild(open);
  return tr;
}

async function load() {
  const d = await ask('/super/data');
  const rows = $('rows');
  rows.innerHTML = '';
  if (!d.sections.length) {
    const tr = document.createElement('tr');
    cell(tr, 'no sections yet', 'mut').colSpan = 5;
    rows.appendChild(tr);
  }
  for (const s of d.sections) rows.appendChild(sectionRow(s));
  const set = $('set');
  if (!set.options.length) for (const ss of d.sets) {
    const o = document.createElement('option');
    o.value = ss.id;
    o.textContent = ss.name + ' · ' + ss.subjects
      + ' subject' + (ss.subjects === 1 ? '' : 's');
    set.appendChild(o);
  }
  if (open_id) await show(open_id);
}

async function show(id) {
  const s = await ask('/super/section?id=' + encodeURIComponent(id));
  open_id = id;
  // The paste box is about to be refilled from the section, so last paste's
  // complaints have to go with it -- five red lines under a textarea whose
  // contents they are no longer about is a screen that looks broken.
  $('ttlist').classList.add('hide');
  $('ttok').classList.add('hide');
  $('tterr').textContent = '';
  $('one').classList.remove('hide');
  $('onename').textContent = s.label;
  $('onesub').textContent = s.members.length + ' member'
    + (s.members.length === 1 ? '' : 's')
    + ' · ' + (s.set || 'no subject set')
    + ' · invite ' + (s.invite || 'none yet')
    + ' · ' + s.timetable.length + ' period'
    + (s.timetable.length === 1 ? '' : 's') + ' set';
  const body = $('members');
  body.innerHTML = '';
  if (!s.members.length) {
    const tr = document.createElement('tr');
    cell(tr, 'nobody has joined yet. Hand out the invite code', 'mut').colSpan = 5;
    body.appendChild(tr);
  }
  for (const m of s.members) {
    const tr = document.createElement('tr');
    cell(tr, m.name, 'name');
    cell(tr, m.roll_no);
    const pill = document.createElement('span');
    pill.className = m.status === 'approved' ? 'pill q' : 'pill';
    pill.textContent = m.status;
    cell(tr, '').appendChild(pill);
    const sel = document.createElement('select');
    sel.setAttribute('aria-label', 'Role for ' + m.name);
    for (const r of ROLES) {
      const o = document.createElement('option');
      o.value = r; o.textContent = r;
      if (r === m.role) o.selected = true;
      sel.appendChild(o);
    }
    sel.onchange = () => run(sel, async () => {
      await ask('/super/role', {id: m.id, role: sel.value});
      await show(open_id);
    }, 'roleerr');
    cell(tr, '').appendChild(sel);
    // Blocking is reversible and never deletes: the row stays, the uploads and
    // the votes stay, and the same button puts them back. That is why it reads
    // Block rather than Remove.
    const blocked = m.status === 'blocked';
    const b = document.createElement('button');
    b.type = 'button';
    b.className = blocked ? 'ghost' : 'adm';
    b.textContent = blocked ? 'Unblock' : 'Block';
    b.onclick = () => run(b, async () => {
      await ask('/super/block', {id: m.id, blocked: !blocked});
      await show(open_id);
    }, 'roleerr');
    cell(tr, '').appendChild(b);
    body.appendChild(tr);
  }

  // Who the registrar lists and this app has never seen. The heading carries
  // the count because that is the number somebody acts on -- "93 not joined"
  // is a morning's chasing and "0" is a section that is all in.
  const gone = s.missing || [];
  $('missinghead').textContent = gone.length
    ? 'Not joined yet - ' + gone.length : 'Not joined yet - none';
  const miss = $('missing');
  miss.innerHTML = '';
  if (!gone.length) {
    const tr = document.createElement('tr');
    cell(tr, 'everybody on the roll list has an account', 'mut').colSpan = 3;
    miss.appendChild(tr);
  }
  for (const m of gone) {
    const tr = document.createElement('tr');
    cell(tr, m.name, 'name');
    cell(tr, m.roll_no);
    cell(tr, m.scholar_no, 'mut');
    miss.appendChild(tr);
  }
  $('known').textContent = s.subjects.length
    ? 'Codes this section knows: ' + s.subjects.map(c => c.code).join(', ')
    : 'This section’s subject set is empty, so every code would be unknown.';
  $('csv').value = s.timetable.map(t => DAYS[t.day] + ',' + t.period + ',' + t.code).join('\n');
}

$('new').onsubmit = e => {
  e.preventDefault();
  run($('mk'), async () => {
    await ask('/super/section',
      {name: $('nm').value, grad_year: $('yr').value, set: $('set').value});
    $('nm').value = ''; $('yr').value = '';
    await load();
  }, 'newerr');
};

$('wr').onclick = () => run($('wr'), async () => {
  $('ttlist').classList.add('hide');
  $('ttok').classList.add('hide');
  try {
    const got = await ask('/super/timetable', {section: open_id, csv: $('csv').value});
    $('ttok').textContent = got.written + ' periods written, and put on '
      + got.members + (got.members === 1 ? ' week.' : ' weeks.');
    $('ttok').classList.remove('hide');
    await show(open_id);
  } catch (e) {
    if (!e || !e.lines) throw e;
    // Every complaint the parser made, in the order they were pasted.
    const ul = $('ttlist');
    ul.innerHTML = '';
    for (const line of e.lines) {
      const li = document.createElement('li');
      li.textContent = line;
      ul.appendChild(li);
    }
    ul.classList.remove('hide');
    throw {error: e.error};
  }
}, 'tterr');

load().catch(() => { $('rows').textContent = 'could not reach the server'; });
</script>
""".replace("__ROLES__", json.dumps(list(ROLES))).replace("__DAYS__", json.dumps(DAYS))


EXPLAIN_PROMPT = """A student is reading their lecture notes and highlighted a passage they do not
understand. Explain just that passage.

Rules:
- 3-5 sentences. This is a quick doubt, not a lecture.
- Plain language first, then the technical statement.
- If it is a formula, say what each symbol means and when you would use it.
- Maths as LaTeX: $...$ inline, $$...$$ display.
- Answer only what was highlighted. Do not summarise the whole note.
- If the passage is too fragmentary to explain, say so and ask what specifically is unclear."""


# The other side of the same coin. Explain is handed a passage with the note
# around it; this is handed a sentence somebody typed at one in the morning and
# nothing else, so the one rule Explain does not need is the rule that matters
# most here: say when the question alone is not enough to answer, instead of
# inventing the lecture it was about.
DOUBT_PROMPT = """A student asked their classmates a question about a lecture. Answer it.

Rules:
- 3-6 sentences. A classmate answering at one in the morning, not a textbook.
- Plain language first, then the technical statement.
- Maths as LaTeX: $...$ inline, $$...$$ display.
- You were given the question and nothing else -- no slides, no recording, no
  note. If answering needs something you were not given, say which thing, and
  answer the part you can.
- Never guess at what a specific lecturer said or did. You do not know."""

# Groq's free tier, the largest model on it. Named here rather than only in
# argparse because build_server is handed an args by the tests too, and a
# default that lives in the parser is a default those never get.
AI_MODEL = "llama-3.3-70b-versatile"


def groq(system, user, model, max_tokens=1200):
    """One call to Groq's OpenAI-shaped endpoint, over stdlib.

    Every other API in this file arrives as a client library. This one is a
    single POST with two strings in and one string out, and urllib is already
    imported three routes down for the Google callback -- a dependency for this
    would be a dependency for nothing.

    Groq's free tier is rate limited by the day rather than by the dollar, so
    there is no price_of() here and nothing to log a cost for. What limits it
    is the cache and the session budget, in the callers.
    """
    import urllib.error
    import urllib.request

    key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("no GROQ_API_KEY set")
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.3,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            got = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # Groq says why in the body -- a bad key, an unknown model, the daily
        # limit. Losing that and reporting "HTTP 400" to a phone would make all
        # three look like the same problem.
        detail = (e.read() or b"").decode("utf-8", "replace")[:400]
        raise RuntimeError(f"groq {e.code}: {detail}") from None
    text = (got["choices"][0]["message"]["content"] or "").strip()
    if not text:
        raise RuntimeError("groq returned nothing")
    return text


def build_server(args):
    """Everything serve() needs, assembled but not yet listening.

    Split out from serve() so a test can drive the real handler over a real
    socket. An auth gate is only worth what it does in the server that actually
    runs, so the tests exercise this one rather than a stand-in.
    """
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    # An ungated server must not reach the wifi by accident: --no-auth is for
    # this laptop, so it binds to loopback unless --host says otherwise.
    if getattr(args, "host", None) is None:
        args.host = "127.0.0.1" if args.no_auth else "0.0.0.0"

    root = Path(__file__).resolve().parent
    global LOG_PATH
    Path(args.library).mkdir(parents=True, exist_ok=True)
    export(args)  # always serve the current library

    LOG_PATH = Path(args.library) / "recarve.log"
    cache_dir = Path(args.library) / ".explains"
    cache_dir.mkdir(parents=True, exist_ok=True)
    inbox = Path(args.library) / ".inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    # Explain and the doubt bot draw on one budget because they draw on one
    # free tier: Groq counts requests per day, and it does not care which of
    # the two screens a request came from.
    budget = {"left": args.max_explains}
    ai_model = getattr(args, "ai_model", AI_MODEL)
    jobs = Jobs(args)
    logins = Limiter()
    # Its own limiter, not a shared one with its own key prefix: a classmate
    # fat-fingering their password must not spend a try the super admin needs,
    # and the two windows are free to diverge later without either moving the
    # other.
    supers = Limiter()

    secret = b"" if args.no_auth else session_secret()
    # Minted even under --no-auth: the worker routes are gated on it whatever
    # else this server is doing, so there is never a mode in which they are open.
    worker_key = worker_token()
    if not args.no_auth:
        with db() as conn:
            first = db_bootstrap(conn)
            # Files that predate the database have nobody on them. Adopt them
            # once, in the admin's name, so every item on a shelf says who put
            # it there rather than half of them saying nothing.
            adopted = db_backfill(conn, args.library)
        if adopted:
            log(f"registered {adopted} existing file(s) under the admin", "backfill")
        if first:
            log(f"nobody has joined yet, whoever uses invite code {first} first "
                f"becomes the admin", "invite")

    # The queue lives in memory but the audio lives on disk, so a restart used
    # to strand an upload forever. Anything still in the inbox gets re-queued;
    # transcription resumes from its checkpoint rather than starting over.
    for leftover in sorted(inbox.glob("*")):
        if leftover.suffix.lower() in AUDIO_EXTS:
            code = leftover.name.split("-", 1)[0]
            if code in SUBJECTS:
                jobs.add(leftover, code, "audio")
                log(f"re-queued {leftover.name} from a previous run", "resume")

    # The only two things the page ever wants off disk: the built page itself
    # and the uploads it links to. Approval says you are a classmate, not that
    # you may read .env -- which holds the API key and the cookie secret -- or
    # .git, or the source. Serving the repo is how both walked out.
    servable = (Path(args.out).resolve().parent, Path(args.library).resolve())

    class Handler(SimpleHTTPRequestHandler):
        # HTTP/1.0 -- the default -- is a new TCP connection and a new thread
        # for every request, and the page polls /jobs every two seconds on top
        # of its own assets. A hundred phones is fifty connections a second
        # that way, and the reel lands on Tuesday. Keeping the connection means
        # every response must say how long it is, which is what end_headers
        # below is for.
        protocol_version = "HTTP/1.1"
        # And a kept connection must not be kept forever: a phone that goes
        # into a pocket mid-poll would otherwise hold its thread until the
        # process restarts.
        timeout = 30

        def log_message(self, fmt, *a):
            if args.verbose:
                super().log_message(fmt, *a)

        def translate_path(self, path):
            if path.split("?")[0] in ("/", "/index.html"):
                return str(args.out)
            return super().translate_path(path)

        def end_headers(self):
            """The last word on whether this connection is kept, for every
            response the server makes -- ours, the stdlib's and its errors'.

            Only a GET is kept. A POST refused before its body was read -- and
            half of them are, by --no-auth, by admins-only, by the size caps --
            leaves those bytes on the socket, where the next request line
            should be, and the request after it would be parsed out of
            somebody's JSON. A HEAD is the same shape of trouble from the other
            end: the senders below write a body it will not read. Neither is
            worth proving thirty handlers right for, and neither is what the
            reuse is for: the page, its assets and the job poll are all GETs.
            """
            if self.command != "GET":
                self.close_connection = True
            # Say so, rather than hanging up on a client that has been told
            # nothing and is entitled to assume the connection stays. The
            # stdlib's send_error says it too, so its 404s carry the header
            # twice -- two headers that agree, which every client reads as the
            # one thing they both say, and cheaper than reaching into the
            # header buffer to find out whether it has been said already.
            if self.close_connection:
                self.send_header("Connection", "close")
            super().end_headers()

        def send_head(self):
            """Every static file, GET and HEAD alike, comes through here."""
            f = Path(self.translate_path(self.path)).resolve()
            if not any(f == d or d in f.parents for d in servable):
                return self.send_error(404)
            return super().send_head()

        def parse_request(self):
            """The gate. Every request passes through here, whatever the verb,
            before BaseHTTPRequestHandler dispatches it.

            Gating here rather than at the top of each handler is the whole
            point: a route added later is shut by default and has to be named in
            PUBLIC_PATHS to be reachable, so forgetting about auth leaves the
            new endpoint closed rather than open. Returning False is the
            documented way to say "a response has already been sent".
            """
            if not super().parse_request():
                return False
            self.me = None
            # A worker is not a member. It presents a shared token and gets the
            # queue and nothing else, and it is settled here -- before the
            # cookie is read, before --no-auth waves anyone through, and with
            # its own `return` either way. That is what keeps the two kinds of
            # credential from leaking into each other: a session never reaches
            # the branch that could make it a worker, and a token never reaches
            # the branch that would give it a profile.
            if self.path.split("?")[0].startswith(WORKER_PREFIX):
                sent = self.headers.get("X-Worker-Token", "").encode("utf-8", "replace")
                if not hmac.compare_digest(sent, worker_key.encode()):
                    # The same refusal for a wrong token as for none at all.
                    self.reply(403, {"error": "you are not approved to read this yet"})
                    return False
                return True
            # And the super admin is settled here, for exactly the reason the
            # worker above is: before the cookie is read, before --no-auth
            # waves anyone through, with its own `return` either way. A student
            # session never reaches the branch that could make it a super
            # admin, and a super admin's cookie never reaches the branch that
            # would hand it a profile.
            where = self.path.split("?")[0]
            if where == SUPER_PREFIX or where.startswith(SUPER_PREFIX + "/"):
                return self.super_gate(where)
            if args.no_auth:
                return True
            try:
                self.me = self.principal()
            except psycopg.Error as e:
                log(f"cannot reach the database: {e}", "auth")
                self.reply(503, {"error": "the library is offline"})
                return False

            path = self.path.split("?")[0]
            if path in PUBLIC_PATHS:
                return True
            if self.me and self.me["status"] == "approved":
                # A member who has never set a password has exactly one door,
                # and it is the one that sets it. Here rather than by showing a
                # different screen: somebody mid-change is holding a valid
                # cookie, and a screen that hides the library is one curl away
                # from every route behind it.
                if self.me["must_set"] and path not in PASSWORD_PATHS:
                    if path in ("/", "/index.html"):
                        self.send_html(GATE_PAGE.replace("__BODY__", SET_PASSWORD_BODY))
                        return False
                    self.reply(403, {"error": "pick a password before anything "
                                              "else opens", "set_password": True})
                    return False
                # Approved says you are a classmate. Role says what you may do
                # with that, and this is where a student is refused -- before
                # dispatch, so curl is refused exactly as the app's own screens
                # are, and so a route added later starts out shut.
                need = ROLE_REQUIRED.get(path)
                if need and RANK.get(self.me["role"], 0) < RANK[need]:
                    self.reply(403, {"error": f"{path} needs {need} access",
                                     "required": need, "role": self.me["role"]})
                    return False
                return True
            # The front page is the one thing an outsider may see, and only so
            # they can ask to be let in.
            if path in ("/", "/index.html"):
                body = self.join_screen() if not self.me else (
                    BLOCKED_BODY if self.me["status"] == "blocked" else WAIT_BODY)
                self.send_html(GATE_PAGE.replace("__BODY__", body))
                return False
            self.reply(403, {"error": "you are not approved to read this yet"})
            return False

        def join_screen(self):
            """The front door, with the invite link's code already in the box.

            The whole point of the link is a cold tap from WhatsApp: no
            session, no app, one hand, and nothing to copy across. Everything
            it saves is typing -- the code still has to survive join_with_invite
            on the way through.
            """
            return join_body()

        def principal(self):
            """Whose session this is, re-read from the database at least every
            PRINCIPAL_TTL seconds.

            Never cached in the cookie: blocking someone has to take effect on
            their next tap, not whenever their cookie happens to expire. Ten
            seconds keeps that promise -- a block still lands while the phone is
            still in the same hand -- and buys the thing the cookie could not:
            one Postgres connection per person per ten seconds instead of one
            per request, on a server that opens a fresh connection every time
            because a pooled one would carry the last student's identity into
            the next student's queries.

            An admin's own actions do not wait even that long. Everything that
            writes a status, a role or a password calls forget_principal, so
            approving, blocking and promoting are felt on the next tap.
            """
            jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            morsel = jar.get(SESSION_COOKIE)
            profile_id = unsign_session(morsel.value, secret) if morsel else None
            if not profile_id:
                return None
            key = profile_id.lower()
            now = time.monotonic()
            with _principals_lock:
                seen = _principals.get(key)
            if seen and now - seen[0] < PRINCIPAL_TTL:
                return seen[1]
            with db(profile_id) as conn:
                me = db_principal(conn, profile_id)
            with _principals_lock:
                # One entry per phone that has ever signed in to this process,
                # which is the class. Nobody unsigned reaches here: the cookie
                # was checked against the secret two lines up.
                _principals[key] = (now, me)
            return me

        # False until super_gate says otherwise, so a handler that reads it on
        # a request that never went through the gate reads "no" rather than
        # raising -- and a route added under /super later starts out shut.
        is_super = False

        def client_ip(self):
            """Which visitor this is, as far as a rate limiter can tell.

            Behind the Cloudflare Tunnel every socket comes from 127.0.0.1, so
            without a forwarded address every phone on the internet shares one
            bucket and the limits stop meaning anything. CF-Connecting-IP is
            the one the tunnel writes itself -- and overwrites, so a client
            cannot choose it -- and X-Forwarded-For is the fallback for an
            install that is not behind one.

            ponytail: off the tunnel a client can write either header, so this
            is a brake on a naive grinder rather than a wall.
            """
            for header in ("CF-Connecting-IP", "X-Forwarded-For"):
                got = self.headers.get(header, "").split(",")[0].strip()
                if got:
                    return got[:64]
            return self.client_address[0]

        def super_gate(self, where):
            """/super's own door, settled before the student gate is reached.

            Everything about this is beside the student path rather than on top
            of it. The cookie is signed with its own key, so a student's cookie
            does not verify here and this one does not verify there -- that is
            arithmetic, not a check somebody has to remember. It is also
            Path=/super, so a browser never sends it to a student route at all,
            and the student gate never even sees it.

            Returning False is "a response has already been sent".
            """
            # No pair, no surface -- and --no-auth has no database to show and
            # no real secret to sign with, so it has none either. A login box
            # here would tell whoever found the path that there is something
            # behind it; a 404 is what every other unrouted path answers.
            admin = super_admin()
            if args.no_auth or not admin:
                self.send_error(404)
                return False
            email = admin[0]
            jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            morsel = jar.get(SUPER_COOKIE)
            until = unsign_session(morsel.value, super_secret(secret, email)) if morsel else None
            # The signature only says we minted it; the deadline inside says it
            # is still ours to honour. A cookie the browser kept past Max-Age
            # stops working here too, which is the half a browser cannot be
            # asked to enforce.
            self.is_super = bool(until and until.isdigit() and int(until) > time.time())
            if where == SUPER_PREFIX + "/login":
                return True          # the one route that is reached without it
            if where == SUPER_PREFIX and self.command == "GET":
                return True          # the screen serves whichever body fits
            if not self.is_super:
                return self.reply(403, {"error": "sign in at /super first"}) or False
            # SameSite=Strict below is what stops another site's page carrying
            # this cookie into a write. This is the second wall, and it is the
            # cheaper one: a cross-site <form> can send text/plain shaped like
            # JSON, but it cannot set a content type without a preflight the
            # browser would have to ask us to allow, and we never do.
            if self.command == "POST" and "application/json" not in \
                    self.headers.get("Content-Type", ""):
                return self.reply(415, {"error": "this route takes JSON"}) or False
            return True

        def do_super_login(self):
            """The password, once, on its own limiter.

            Both halves are compared whatever the first one says, and both with
            compare_digest: an early return on a wrong email is a way to find
            out the right one, and a byte-at-a-time == is a slower way to find
            out the password.
            """
            email, password = super_admin()
            try:
                req = self.body(4000)
                if req is None:
                    return
                mine = (f"super:{self.client_ip()}", SUPER_TRIES, "address")
                everybody = (SUPER_TOTAL_KEY, SUPER_TRIES_TOTAL, "install")
                for key, limit, what in (mine, everybody):
                    if supers.locked(key, limit):
                        log(f"locked out for {LOGIN_WINDOW // 60} minutes "
                            f"({limit} wrong per {what})", "super")
                        return self.reply(429, {
                            "error": f"too many wrong tries - {limit} per {what}, "
                                     f"then a {LOGIN_WINDOW // 60} minute wait"})
                sent_email = (req.get("email") or "").strip().lower().encode()
                sent_password = (req.get("password") or "")[:200].encode()
                # & and not `and`: both comparisons run either way.
                ok = (hmac.compare_digest(sent_email, email.lower().encode())
                      & hmac.compare_digest(sent_password, password.encode()))
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            if not ok:
                supers.fail(mine[0])
                supers.fail(everybody[0])
                log(f"a wrong password from {self.client_ip()}", "super")
                # One sentence for both halves, for the same reason /login has
                # one for a wrong roll number and a wrong password.
                return self.reply(403, {"error": "that email and password do not match"})
            supers.clear(mine[0], everybody[0])
            log("signed in", "super")
            until = int(time.time()) + SUPER_SESSION
            # Path=/super so it is never sent anywhere else; Secure so it is
            # never sent in the clear; Strict so no other site's page can spend
            # it; HttpOnly so a script cannot read it back out.
            cookie = (f"{SUPER_COOKIE}={sign_session(until, super_secret(secret, email))}; "
                      f"Path={SUPER_PREFIX}; HttpOnly; Secure; SameSite=Strict; "
                      f"Max-Age={SUPER_SESSION}")
            return self.reply(200, {"ok": True}, cookie=cookie)

        def super_do(self, fn, cap=4000):
            """Run one /super handler on the owning connection and answer.

            The five below differ by three lines each and agree about every
            refusal, which is the point: a ValueError out of the db_ functions
            is a sentence written for a person and becomes a 400, and nothing
            else is allowed to become a 200.
            """
            try:
                req = self.body(cap) if self.command == "POST" else {}
                if req is None:
                    return
                with db() as conn:
                    return self.reply(200, fn(conn, req))
            except SuperRefused as e:
                return self.reply(400, {"error": str(e), "lines": e.lines})
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except psycopg.errors.RaiseException as e:
                return self.reply(400, {"error": str(e).splitlines()[0]})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})

        def super_asked(self, name):
            """One id off the query string, as a uuid or as a refusal."""
            import urllib.parse

            return a_uuid(urllib.parse.parse_qs(
                self.path.partition("?")[2]).get(name, [""])[0])

        def do_super_data(self):
            return self.super_do(lambda conn, req: {
                "sections": db_sections(conn), "sets": db_subject_sets(conn)})

        def do_super_section(self):
            return self.super_do(
                lambda conn, req: db_section(conn, self.super_asked("id")))

        def do_super_create(self):
            def make(conn, req):
                label = db_create_section(conn, req.get("name"), req.get("grad_year"),
                                          a_uuid(req.get("set")))
                log(f"created {label}", "super")
                return {"label": label}
            return self.super_do(make)

        def do_super_role(self):
            def grant(conn, req):
                target = a_uuid(req.get("id"))
                # actor None: the super admin is in no section and holds no
                # profile, so there is no row they could be demoting. The
                # self-check inside compares against a string that is not a
                # uuid and the 0011 trigger passes an owning connection
                # through, which is the same door db_join's promotion uses.
                role = db_set_role(conn, None, target, (req.get("role") or "").strip())
                log(f"{target} is now {role}", "super")
                return {"role": role}
            return self.super_do(grant)

        def do_super_block(self):
            """Shut somebody out of any section, or let them back in.

            The same db_set_status the class admin's own screen calls, so there
            is one rule about what blocking means and one place it is written.
            What differs is who may reach it: a class admin may only aim it
            inside their own section, and this surface is above the section
            line, which is the whole reason it exists -- nine of the ten
            sections have no admin yet, so without this there is nobody at all
            who can shut a bad account out of them.

            actor None for the same reason db_set_role passes it: the super
            admin holds no profile, so there is no row they could be blocking
            by accident, and the self-check compares against a string that is
            not a uuid. The 0011 trigger still stands behind it.
            """
            def shut(conn, req):
                target = a_uuid(req.get("id"))
                blocked = bool(req.get("blocked", True))
                status = db_set_status(conn, None, target, blocked)
                log(f"{target} is now {status}", "super")
                return {"status": status}
            return self.super_do(shut)

        def do_super_invite(self):
            def mint(conn, req):
                section = a_uuid(req.get("section"))
                code = db_mint_invite(conn, section)
                log(f"minted an invite for {section}", "super")
                return {"code": code}
            return self.super_do(mint)

        def do_super_timetable(self):
            """A pasted week, read entirely before any of it is written.

            parse_timetable is the same pure function the CLI calls -- a second
            parser here would be a second set of rules about what a day is, and
            the one that disagreed would be whichever was not being read.
            """
            def write(conn, req):
                section = a_uuid(req.get("section"))
                # Also what proves the section exists, and what says which
                # codes are known -- the set it follows, not every subject.
                known = [c["code"] for c in db_section(conn, section)["subjects"]]
                rows, errors = parse_timetable(req.get("csv") or "", known)
                if errors:
                    # Raised, not returned, so it cannot be mistaken for a
                    # written week -- and carrying every complaint at once.
                    raise SuperRefused("nothing was written - fix these lines "
                                       "and paste again", errors)
                written, people = db_set_section_timetable(conn, section, rows)
                log(f"wrote {written} periods for {section}, "
                    f"onto {people} weeks", "super")
                return {"written": written, "members": people}
            return self.super_do(write, cap=20000)

        def do_GET(self):
            # Before anything else, because these paths must never fall through
            # to a student route or to the static file server.
            where = self.path.split("?")[0]
            if where == SUPER_PREFIX:
                return self.send_html(SUPER_PAGE.replace(
                    "__BODY__", SUPER_BODY if self.is_super else SUPER_LOGIN_BODY))
            if where == SUPER_PREFIX + "/data":
                return self.do_super_data()
            if where == SUPER_PREFIX + "/section":
                return self.do_super_section()
            if where.startswith(SUPER_PREFIX + "/"):
                return self.send_error(404)
            if self.path == "/sw.js":
                return self.send_js(SW_JS)
            if where == "/auth/google":
                return self.do_google_start()
            if where == "/auth/google/callback":
                return self.do_google_callback()
            if self.path.split("?")[0] == "/login":
                return self.send_html(GATE_PAGE.replace("__BODY__", with_google(LOGIN_BODY)))
            if self.path == "/data":
                subjects = build_data(args.library, args.out.parent)
                # `now` is this machine's clock, and the mtimes in `subjects`
                # are the same clock. Home's "new since you last looked" is a
                # comparison between two of these, never against the phone's
                # own clock, which is minutes out often enough to matter.
                out = {"subjects": subjects, "now": int(time.time()),
                       "codes": [{"code": c, "name": n.replace("-", " ")}
                                 for c, (n, _) in SUBJECTS.items()]}
                # Disk says what exists; the database says who added it and how
                # the class voted. --no-auth has neither a database nor anyone
                # to credit, which is the whole point of --no-auth.
                if self.me:
                    out["role"] = self.me["role"]
                    with db(self.me["id"]) as conn:
                        apply_meta(subjects, *db_meta(conn, self.me["id"]))
                        # Home's extras are extras. A migration not yet applied
                        # on this machine used to kill the handler mid-reply,
                        # and a dropped connection reads to the phone as "no
                        # server": stale baked data, no uploads, no votes, no
                        # jobs. The page already copes with a missing
                        # timetable, so a failure here costs the Today section
                        # and nothing else.
                        try:
                            out["timetable"] = db_timetable(conn, self.me["id"])
                            # Everything Home needs rides on the request it
                            # already makes. A second round trip for a number
                            # is a second thing that can be slow on a phone in
                            # a corridor.
                            # The notice board rides on the same request:
                            # Home draws it without asking for anything, and
                            # Campus is painted from what is already held.
                            out["announcements"] = db_announcements(
                                conn, self.me["id"])
                            # And attendance, on the same request: Home marks
                            # today's classes and the subject screen shows the
                            # number, and neither may cost a round trip of its
                            # own. It is read on the caller's connection, so
                            # the policy hands back their marks and nobody
                            # else's -- this payload is never anyone's but the
                            # person who asked for it.
                            out["attendance"] = db_attendance(
                                conn, self.me["id"])
                            # Saved notes, on the same request: private to the
                            # person who asked, same as attendance above.
                            out["bookmarks"] = db_bookmarks(conn, self.me["id"])
                            # And the paper counts, on the same request: the
                            # subjects screen said "Nothing yet" under a course
                            # holding thirty past papers, because it only ever
                            # counted what this class had made itself.
                            out["paper_counts"] = db_paper_counts(conn)
                            if self.me["admin"]:
                                out["pending"] = len(db_pending(conn))
                        except psycopg.Error as e:
                            log(f"Home's extras are unavailable: {e}", "data")
                return self.reply(200, out)
            if self.path == "/me":
                if not self.me:
                    return self.reply(404, {"error": "this server is running with --no-auth"})
                with db(self.me["id"]) as conn:
                    out = db_contributions(conn, self.me["id"])
                    # Who they are, next to what they have put in. One request:
                    # the Me tab is opened between classes on mobile data like
                    # every other screen, and identity and points are one card.
                    out.update(db_profile(conn, self.me["id"]))
                    out["section"] = SECTION
                    # The Me tab is also where the admin does the two admin
                    # things. Gated twice on purpose: here, and by the invites
                    # policy that gives a member no read at all.
                    out["admin"] = bool(self.me["admin"])
                    out["role"] = self.me["role"]
                    out["invite"] = db_invite(conn) if self.me["admin"] else None
                    if self.me["admin"]:
                        # The badge on the Admin row: a queue you have to open
                        # a second screen to discover is a queue that waits.
                        try:
                            out["pending"] = len(db_pending(conn))
                        except psycopg.Error as e:
                            log(f"cannot count the queue: {e}", "me")
                    return self.reply(200, out)
            if self.path == "/standings":
                if not self.me:
                    return self.reply(404, {"error": "this server is running "
                                                     "with --no-auth"})
                # Read as themselves: standings is security_invoker, so a
                # session that may not see the class does not get a board of it.
                # Deliberately not in ROLE_REQUIRED -- knowing who has put the
                # most in is not a privilege, and a student who cannot upload
                # can still see who did.
                with db(self.me["id"]) as conn:
                    return self.reply(200, db_standings(conn, self.me["id"]))
            if self.path == "/campus":
                if not self.me:
                    return self.reply(404, {"error": "this server is running "
                                                     "with --no-auth"})
                # One request for the whole tab. Read as themselves, so a
                # hidden club is only in the payload of somebody who can
                # unhide it -- the policies decide that, not this handler.
                with db(self.me["id"]) as conn:
                    return self.reply(200, {
                        "clubs": db_clubs(conn),
                        "events": db_events(conn, self.me["id"]),
                        "places": db_places(conn),
                        # Null key and null provider is the honest state, not
                        # an error: the places list below the map is useful
                        # with no provider at all.
                        "maps": maps_config(),
                        "role": self.me["role"]})
            if self.path.split("?")[0] == "/doubts":
                return self.do_doubts_get()
            if self.path.split("?")[0] == "/posts":
                return self.do_posts_get()
            if self.path.split("?")[0] == "/chat":
                return self.do_chat_get()
            if self.path.split("?")[0] == "/confession-author":
                return self.do_confession_author()
            if self.path.split("?")[0] == "/papers":
                if not self.me:
                    return self.reply(404, {"error": "this server is running "
                                                     "with --no-auth"})
                # Named /papers rather than /archive because the library is
                # served statically from the same root, and the files live at
                # /archive/... -- a JSON route on that prefix would shadow
                # every document it describes.
                import urllib.parse
                code = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query).get("code", [""])[0]
                # No code at all is the papers screen asking for the whole
                # archive. An unknown code is still a mistake worth naming --
                # the empty string is a question, "CS9999" is a typo.
                if code and code not in SUBJECTS:
                    return self.reply(400, {"error": "unknown subject"})
                with db(self.me["id"]) as conn:
                    return self.reply(200,
                                      {"papers": db_papers(conn, code or None)})
            if self.path == "/jobs":
                return self.reply(200, {"jobs": jobs.snapshot()})
            if self.path.split("?")[0] == "/worker/audio":
                return self.do_worker_audio()
            if self.path == "/log":
                try:
                    tail = LOG_PATH.read_text().splitlines()[-200:]
                except OSError:
                    tail = []
                return self.reply(200, {"lines": tail})
            if self.path == "/admin":
                if not self.is_admin():
                    return self.reply(403, {"error": "admins only"})
                return self.send_html(GATE_PAGE.replace("__BODY__", ADMIN_BODY))
            if self.path == "/pending":
                if not self.is_admin():
                    return self.reply(403, {"error": "admins only"})
                with db(self.me["id"]) as conn:
                    pending = db_pending(conn)
                    members = db_members(conn)
                    reports = db_reports(conn)
                    return self.reply(200, {
                        "pending": pending, "members": members,
                        "reports": reports, "me": self.me["id"],
                        # The code is read on the admin's own connection, where
                        # the invites policy allows it and a member's does not.
                        "invite": db_invite(conn),
                        # Counts, so the queue is obvious before anything is
                        # scrolled to. Computed from the lists that were fetched
                        # anyway rather than from three more round trips.
                        "counts": {"pending": len(pending), "members": len(members),
                                   "blocked": sum(1 for m in members
                                                  if m["status"] == "blocked"),
                                   "reports": sum(1 for r in reports
                                                  if r["status"] != "removed")},
                        # This machine's clock, which is the one the timestamps
                        # above are on. "2 days ago" against the handset's own
                        # clock is minutes out often enough to read wrong.
                        "now": int(time.time())})
            return super().do_GET()

        def at_least(self, role):
            """Does whoever is asking stand at or above that rung?

            The same question the gate asks before dispatch, in the same terms,
            for the handful of handlers that ask it a second time inside
            themselves. Spelled once here so that adding a role is a word in
            ROLES rather than a hunt through every `== 'admin'` in the file --
            and so an unknown role, or nobody at all, is refused rather than
            being compared against and accidentally passing.
            """
            return bool(self.me) and RANK.get(self.me["role"], -1) >= RANK[role]

        def is_admin(self):
            return self.at_least("admin")

        def do_POST(self):
            where = self.path.split("?")[0]
            if where.startswith(SUPER_PREFIX):
                return {SUPER_PREFIX + "/login": self.do_super_login,
                        SUPER_PREFIX + "/section": self.do_super_create,
                        SUPER_PREFIX + "/role": self.do_super_role,
                        SUPER_PREFIX + "/block": self.do_super_block,
                        SUPER_PREFIX + "/invite": self.do_super_invite,
                        SUPER_PREFIX + "/timetable": self.do_super_timetable,
                        }.get(where, lambda: self.send_error(404))()
            if self.path == "/join":
                return self.do_join()
            if self.path == "/login":
                return self.do_login()
            if self.path == "/logout":
                # The cookie is signed, not stored, so leaving is forgetting it.
                return self.reply(200, {"ok": True}, cookie=(
                    f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"))
            if self.path == "/password":
                return self.do_password()
            if self.path == "/reset":
                return self.do_reset()
            if self.path == "/approve":
                return self.do_approve()
            if self.path == "/role":
                return self.do_role()
            if self.path == "/block":
                return self.do_block()
            if self.path == "/remove":
                return self.do_remove()
            if self.path == "/rename":
                return self.do_rename()
            if self.path == "/remove-lecture":
                return self.do_remove_lecture()
            if self.path == "/profile":
                return self.do_profile()
            if self.path == "/upload":
                return self.do_upload()
            if self.path == "/worker/claim":
                return self.do_worker_claim()
            if self.path == "/worker/progress":
                return self.do_worker_progress()
            if self.path == "/worker/done":
                return self.do_worker_done()
            if self.path == "/worker/failed":
                return self.do_worker_failed()
            if self.path == "/vote":
                return self.do_vote()
            if self.path == "/doubts":
                return self.do_doubts()
            if self.path == "/doubts/ai":
                return self.do_doubts_ai()
            if self.path == "/posts":
                return self.do_posts()
            if self.path == "/chat":
                return self.do_chat()
            if self.path == "/bookmark":
                return self.do_bookmark()
            if self.path == "/attendance":
                return self.do_attendance()
            if self.path == "/cancelled":
                return self.do_cancelled()
            if self.path == "/announce":
                return self.do_announce()
            if self.path in ("/club", "/event", "/place"):
                return self.do_campus(self.path[1:])
            if self.path == "/read":
                return self.do_read()
            if self.path == "/revise":
                return self.do_revise()
            if self.path != "/explain":
                return self.send_error(404)
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n > 20000:
                    return self.reply(413, {"error": "selection too long"})
                req = json.loads(self.rfile.read(n) or b"{}")
                text = (req.get("text") or "").strip()[:8000]
                if not text:
                    return self.reply(400, {"error": "nothing selected"})
                # Compute once, serve many. Two students highlighting the same
                # formula must cost one API call, not two -- with 110 classmates
                # this cache is the difference between pennies and a real bill.
                key = hashlib.sha256(text.encode()).hexdigest()[:32]
                hit = cache_dir / f"{key}.md"
                if hit.exists():
                    return self.reply(200, {"text": hit.read_text(), "cached": True})

                if budget["left"] <= 0:
                    return self.reply(429, {"error": "explain limit reached for this session"})
                budget["left"] -= 1

                out = groq(EXPLAIN_PROMPT,
                           f"From the note \"{req.get('title', '')}\":\n\n{text}",
                           ai_model)
                hit.write_text(out)
                log(f"{budget['left']} left, cached {key[:8]}", "explain", 1)
                self.reply(200, {"text": out})
            except Exception as e:
                # Surface the real reason on the phone; a silent failure here is
                # indistinguishable from a network problem.
                self.reply(500, {"error": f"{type(e).__name__}: {e}"})

        def do_join(self):
            """The only way to acquire a session. Public by necessity."""
            if args.no_auth:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            if self.me:
                return self.reply(400, {"error": "you have already joined"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n > 4000:
                    self.close_connection = True  # body left unread; do not reuse
                    return self.reply(413, {"error": "too much"})
                req = json.loads(self.rfile.read(n) or b"{}")
                name = (req.get("name") or "").strip()[:80]
                roll = (req.get("roll_no") or "").strip()[:40]
                phone = (req.get("phone") or "").strip()[:32]
                code = (req.get("code") or "").strip()[:64]
                password = (req.get("password") or "")[:200]
                if not (name and roll and phone and code and password.strip()):
                    return self.reply(400, {"error": "name, roll number, phone number, "
                                                     "password and invite code are all "
                                                     "required"})
                try:
                    phone = normalise_phone(phone)
                    # Before the invite code is spent, so a refused password
                    # does not cost a use off a code somebody has to ask for
                    # again -- and so the joiner is told which of the two was
                    # wrong instead of being sent back to WhatsApp.
                    password = check_password(password, roll)
                except ValueError as e:
                    return self.reply(400, {"error": str(e)})
                with db() as conn:
                    got = db_join(conn, code, name, roll, phone, password)
            except psycopg.errors.UniqueViolation:
                return self.reply(409, {"error": "that roll number is already registered"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            if not got:
                # One message for wrong, expired and used-up alike: anything more
                # specific turns this form into an invite-code oracle.
                return self.reply(403, {"error": "that invite code is wrong, expired or used up"})

            profile_id, status, is_admin = got
            log(f"{name} ({roll}) joined as {status}" + (", and is the admin" if is_admin else ""),
                "join")
            cookie = (f"{SESSION_COOKIE}={sign_session(profile_id, secret)}; Path=/; "
                      "HttpOnly; SameSite=Lax; Max-Age=31536000")
            return self.reply(200, {"status": status, "admin": is_admin}, cookie=cookie)

        def do_login(self):
            """A roll number and a password in, the same signed cookie /join
            mints out -- so everything downstream of a session is unchanged.

            Public by necessity, and rate limited because of it: 110 roll
            numbers is a list somebody can type out in an evening.
            """
            if args.no_auth:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            try:
                req = self.body(4000)
                if req is None:
                    return
                roll = (req.get("roll_no") or "").strip()[:40]
                password = (req.get("password") or "")[:200]
                # Behind the tunnel every socket comes from 127.0.0.1, so the
                # forwarded address is the only thing that tells two phones
                # apart at all. client_ip() is where that is read, and it
                # prefers the header the tunnel writes itself.
                #
                # The per-roll limit is the one that protects an account, and
                # nothing the caller sends can move it -- it is keyed on the
                # roll number they are guessing at.
                client = self.client_ip()
                limits = ((f"roll:{roll.upper()}", LOGIN_TRIES, "roll number"),
                          (f"ip:{client}", LOGIN_TRIES_PER_CLIENT, "device"))
                for key, limit, what in limits:
                    if logins.locked(key, limit):
                        # Say the number. "Try again later" is what a locked
                        # out classmate reads as "it is broken".
                        return self.reply(429, {
                            "error": f"too many wrong tries - {limit} per {what}, "
                                     f"then a {LOGIN_WINDOW // 60} minute wait"})
                with db() as conn:
                    got = db_login(conn, roll, password)
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            if not got:
                for key, _, _ in limits:
                    logins.fail(key)
                # One sentence for a wrong password and for a roll number
                # nobody holds. Two would make this form the class list.
                return self.reply(403, {"error": "that roll number and password "
                                                 "do not match"})
            profile_id, status, must_set = got
            logins.clear(*(key for key, _, _ in limits))
            log(f"{roll} signed in" + (", and has no password yet" if must_set else ""),
                "login")
            cookie = (f"{SESSION_COOKIE}={sign_session(profile_id, secret)}; Path=/; "
                      "HttpOnly; SameSite=Lax; Max-Age=31536000")
            # status rides back so the screen knows it is about to land on the
            # waiting room rather than the library. Where it actually lands is
            # still the gate's decision, not this answer's.
            return self.reply(200, {"status": status, "set_password": must_set},
                              cookie=cookie)

        def origin(self):
            """What this server is called from outside, which it cannot see.

            Everything arrives through a tunnel, so Host is the tunnel's name
            and the scheme on the socket is http whatever the phone typed.
            X-Forwarded-Proto is what the tunnel writes; RECARVE_ORIGIN is the
            override for an install that fronts itself differently, and it is
            the one that has to match the redirect URI registered with Google
            character for character.
            """
            fixed = (os.environ.get("RECARVE_ORIGIN") or "").strip()
            if fixed:
                return fixed.rstrip("/")
            proto = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip()
            host = (self.headers.get("X-Forwarded-Host")
                    or self.headers.get("Host") or "").split(",")[0].strip()
            return f"{proto}://{host}"

        def go(self, where, *cookies):
            """A redirect and nothing else. The body of a 302 is never read."""
            try:
                self.send_response(302)
                self.send_header("Location", where)
                for cookie in cookies:
                    self.send_header("Set-Cookie", cookie)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def no_google(self):
            """This install has no Google, said the way every other refusal in
            this file is said.

            Not send_error(). The stdlib's version closes the connection, and
            0021 put this server behind a tunnel that keeps connections alive
            and pools them -- so the socket it closes is one cloudflared hands
            the next request, which then answers 502 instead of 404. Observed
            in production the day this shipped: the first /auth/google was a
            404 and every one after it was a 502. reply() writes its own
            Content-Length and leaves the connection where it found it.
            """
            return self.reply(404, {"error": "this install has no Google sign-in"})

        def google_refused(self, why):
            """Back to the login screen holding the sentence.

            A navigation cannot be answered with JSON the way every other
            refusal here is, and a bare 403 page is how somebody who used their
            personal Gmail decides the app is broken rather than that they
            picked the wrong account.
            """
            import urllib.parse

            log(f"google sign-in refused: {why}", "auth")
            return self.go("/login?e=" + urllib.parse.quote(why),
                           f"{OAUTH_COOKIE}=; Path=/auth/; HttpOnly; Max-Age=0")

        def do_google_start(self):
            """Hand the phone to Google, with a state cookie to know the
            answer by.

            hd asks Google to offer the institute's accounts first. It is a
            courtesy to the student and no part of the check: the callback
            reads the domain off the token Google signed, not off this.
            """
            pair = google_client()
            if args.no_auth or not pair:
                return self.no_google()
            import urllib.parse

            state = secrets.token_urlsafe(16)
            where = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
                "client_id": pair[0],
                "redirect_uri": self.origin() + "/auth/google/callback",
                "response_type": "code", "scope": "openid email",
                "state": state, "hd": GOOGLE_DOMAIN, "prompt": "select_account"})
            # Lax, not Strict: this cookie's whole job is to be sent on the way
            # back from accounts.google.com, and Strict is the setting that
            # withholds it exactly then.
            return self.go(where, f"{OAUTH_COOKIE}={state}; Path=/auth/; "
                                  "HttpOnly; SameSite=Lax; Max-Age=600")

        def do_google_callback(self):
            """Google's answer, turned into the same signed cookie /join and
            /login mint -- so everything downstream of a session is unchanged.

            What is checked, in order: that this is the round trip we started
            (the state cookie), that Google will trade the code with us (the
            client secret), that the address is verified and in the institute's
            Workspace domain, and that its scholar number is on the registrar's
            list. The section is never asked for and never typed.
            """
            pair = google_client()
            if args.no_auth or not pair:
                return self.no_google()
            import urllib.parse
            import urllib.request

            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            morsel = jar.get(OAUTH_COOKIE)
            if not code or not state or not morsel or not hmac.compare_digest(
                    state, morsel.value):
                return self.google_refused("that sign-in did not come back the "
                                           "way it left - try again")
            try:
                body = urllib.parse.urlencode({
                    "code": code, "client_id": pair[0], "client_secret": pair[1],
                    "redirect_uri": self.origin() + "/auth/google/callback",
                    "grant_type": "authorization_code"}).encode()
                with urllib.request.urlopen(urllib.request.Request(
                        "https://oauth2.googleapis.com/token", data=body),
                        timeout=15) as got:
                    claims = google_claims(json.loads(got.read()).get("id_token"))
            except Exception as e:
                log(f"google would not trade the code: {type(e).__name__}: {e}", "auth")
                return self.google_refused("could not reach Google - try again")

            email = (claims.get("email") or "").strip().lower()
            if not claims.get("email_verified"):
                return self.google_refused("Google has not verified that address")
            # hd is present only on a Workspace account and is the domain that
            # owns it. The address is checked too, because a Workspace can hold
            # aliases in other domains and it is the scholar number in front of
            # the @ that this whole door turns on.
            if (claims.get("hd") or "").strip().lower() != GOOGLE_DOMAIN:
                return self.google_refused(f"sign in with your @{GOOGLE_DOMAIN} "
                                           "account, not a personal one")
            scholar = scholar_from_email(email)
            if not scholar:
                return self.google_refused(f"{email} is not a student address")
            try:
                with db() as conn:
                    found = db_google(conn, scholar, email)
            except Exception as e:
                log(f"google sign-in failed: {type(e).__name__}: {e}", "auth")
                return self.google_refused("the library is offline - try again")
            if not found:
                # Not on the list, or on it under somebody else's Google
                # account. One sentence for both: the first is a student the
                # registrar has not filed, the second is a thing for a person
                # to look at, and neither is fixed by typing again.
                return self.google_refused(
                    f"{scholar} is not on the section list - ask an admin for "
                    "an invite code instead")
            profile_id, status, is_admin = found
            log(f"{email} signed in with Google as {status}"
                + (", and is the admin" if is_admin else ""), "login")
            return self.go("/",
                           f"{SESSION_COOKIE}={sign_session(profile_id, secret)}; "
                           "Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000",
                           f"{OAUTH_COOKIE}=; Path=/auth/; HttpOnly; Max-Age=0")

        def do_password(self):
            """Where a member sets their own password, first time or later.

            Not in ROLE_REQUIRED, and it is the one path open to somebody
            mid-change: your own password is not a privilege. What stops it
            being more than that is the database, where "edit own name only"
            pins the row to them and status and role to what they already are.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            try:
                req = self.body(4000)
                if req is None:
                    return
                with db(self.me["id"]) as conn:
                    db_set_password(conn, self.me["id"], req.get("password"))
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            log(f"{self.me['name']} set a password", "login")
            return self.reply(200, {"ok": True})

        def do_reset(self):
            """An admin puts somebody back to their roll number.

            Admin-only twice over: the gate refused everyone else before this
            was reached, and the update runs on the caller's own connection,
            where "admins manage profiles" has to allow it too.
            """
            if not self.is_admin():
                return self.reply(403, {"error": "admins only", "required": "admin"})
            try:
                req = self.body(4000)
                if req is None:
                    return
                target = (req.get("id") or "").strip()
                with db(self.me["id"]) as conn:
                    db_reset_password(conn, target)
            except ValueError as e:
                return self.reply(404, {"error": str(e)})
            except psycopg.errors.InvalidTextRepresentation:
                return self.reply(404, {"error": "no such member"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            log(f"{self.me['name']} reset the password for {target}", "admin")
            return self.reply(200, {"ok": True})

        def do_approve(self):
            if not self.is_admin():
                return self.reply(403, {"error": "admins only"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(n) or b"{}")
                target = (req.get("id") or "").strip()
                with db(self.me["id"]) as conn:
                    published = db_approve(conn, target)
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            log(f"{self.me['name']} approved {target}, publishing {published} upload(s)", "admin")
            return self.reply(200, {"ok": True, "published": published})

        def do_role(self):
            """Move somebody between student, trusted, cr and admin.

            Admin-only twice over: the gate refused everyone else before this
            was reached, and the update inside runs on the caller's own
            connection, where the "admins manage profiles" policy has to allow
            it too.
            """
            if not self.is_admin():
                return self.reply(403, {"error": "admins only", "required": "admin"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n > 4000:
                    self.close_connection = True
                    return self.reply(413, {"error": "too much"})
                req = json.loads(self.rfile.read(n) or b"{}")
                target = (req.get("id") or "").strip()
                role = (req.get("role") or "").strip()
                with db(self.me["id"]) as conn:
                    db_set_role(conn, self.me["id"], target, role)
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except psycopg.errors.RaiseException as e:
                # The 0011 trigger, which is the layer under db_set_role. Its
                # sentence is written for a person, so it is passed through
                # rather than turned into a 500 nobody can act on.
                return self.reply(400, {"error": str(e).splitlines()[0]})
            except psycopg.errors.InvalidTextRepresentation:
                return self.reply(404, {"error": "no such member"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            log(f"{self.me['name']} made {target} {role}", "admin")
            return self.reply(200, {"ok": True, "role": role})

        def do_block(self):
            """Block somebody, or let them back in. Rejecting a joiner is this.

            Admin-only three times over: the gate refused everyone else before
            this was reached, the update runs on the caller's own connection
            where "admins manage profiles" has to allow it, and the trigger in
            0011 refuses an admin who aims it at themselves.
            """
            if not self.is_admin():
                return self.reply(403, {"error": "admins only", "required": "admin"})
            try:
                req = self.body(4000)
                if req is None:
                    return
                target = (req.get("id") or "").strip()
                with db(self.me["id"]) as conn:
                    status = db_set_status(conn, self.me["id"], target,
                                           bool(req.get("blocked", True)))
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except psycopg.errors.RaiseException as e:
                return self.reply(400, {"error": str(e).splitlines()[0]})
            except psycopg.errors.InvalidTextRepresentation:
                return self.reply(404, {"error": "no such member"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            log(f"{self.me['name']} set {target} to {status}", "admin")
            return self.reply(200, {"ok": True, "status": status})

        def do_remove(self):
            """Take a reported file off the shelves."""
            if not self.is_admin():
                return self.reply(403, {"error": "admins only", "required": "admin"})
            try:
                req = self.body(4000)
                if req is None:
                    return
                target = (req.get("id") or "").strip()
                with db(self.me["id"]) as conn:
                    db_remove_material(conn, target)
            except ValueError as e:
                return self.reply(404, {"error": str(e)})
            except psycopg.errors.InvalidTextRepresentation:
                return self.reply(404, {"error": "no such item"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            log(f"{self.me['name']} removed material {target}", "admin")
            return self.reply(200, {"ok": True})

        def do_rename(self):
            """Rename a file, a group of files, or a recorded lecture.

            Admin, the same bar as Remove, and for the same reason: it changes
            what everybody in the section sees on the shelf. Three shapes of
            payload on one route, because they are the three things a Rename
            button sits next to.
            """
            if not self.is_admin():
                return self.reply(403, {"error": "admins only", "required": "admin"})
            try:
                req = self.body(2000)
                if req is None:
                    return
                kind, name = req.get("kind"), (req.get("name") or "").strip()
                with db(self.me["id"]) as conn:
                    if kind == "material":
                        out = db_rename_material(conn, (req.get("id") or "").strip(), name)
                    elif kind == "batch":
                        out = db_rename_batch(conn, (req.get("batch") or "").strip(), name)
                    elif kind == "lecture":
                        out = self.rename_lecture(conn, (req.get("subject") or "").strip(),
                                                  (req.get("title") or "").strip(), name)
                    else:
                        raise ValueError("rename what?")
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except psycopg.errors.InvalidTextRepresentation:
                return self.reply(404, {"error": "no such item"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            log(f"{self.me['name']} renamed a {kind} to {out}", "admin")
            return self.reply(200, {"ok": True, "name": out})

        def rename_lecture(self, conn, code, title, name):
            """A lecture's title IS its note's filename (build_data reads
            md.stem), so the .md moves and the row follows. Saved bookmarks
            are keyed by that same title and would silently vanish, so they
            move too -- on the owner's connection, since each one is private
            to whoever saved it and an admin's own session cannot reach them."""
            if code not in SUBJECTS:
                raise ValueError("no such subject")
            new = clean_name(name)
            if not new:
                raise ValueError("a name cannot be blank")
            folder = subject_dir(args.library, code, "lectures")
            old_md = folder / f"{title}.md"
            if not old_md.is_file():
                raise ValueError("no such lecture")
            new_md = old_md if new == title else dedupe_path(folder / f"{new}.md")
            if new_md == old_md:
                return title
            if not conn.execute("select 1 from lectures where subject_code = %s "
                                "and title = %s", (code, title)).fetchone():
                raise ValueError("no such lecture")
            # Disk first, row second, disk undone if the row refuses: never a
            # row naming a note that is not there.
            old_md.rename(new_md)
            try:
                conn.execute("update lectures set title = %s where subject_code = %s "
                             "and title = %s", (new_md.stem, code, title))
            except Exception:
                new_md.rename(old_md)
                raise
            with db() as owner:
                owner.execute("update bookmarks set title = %s where subject_code = %s "
                              "and title = %s", (new_md.stem, code, title))
            return new_md.stem

        def do_remove_lecture(self):
            """Take a recorded lecture's note off the shelves.

            The one route reachable straight from the subject page rather than
            waiting on a report: a student cannot report a lecture (reports
            point at materials only), and there was otherwise no way at all to
            take back a bad recording once it was made.
            """
            if not self.is_admin():
                return self.reply(403, {"error": "admins only", "required": "admin"})
            try:
                req = self.body(1000)
                if req is None:
                    return
                code = (req.get("subject") or "").strip()[:32]
                title = (req.get("title") or "").strip()[:200]
                with db(self.me["id"]) as conn:
                    db_remove_lecture(conn, code, title)
                # The row is gone; the file on disk is what build_data actually
                # reads, and it would otherwise reappear on the next request as
                # though nothing had happened.
                md = subject_dir(args.library, code, "lectures") / f"{title}.md"
                md.unlink(missing_ok=True)
            except ValueError as e:
                return self.reply(404, {"error": str(e)})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            log(f"{self.me['name']} removed lecture {code}/{title}", "admin")
            return self.reply(200, {"ok": True})

        def do_profile(self):
            """A member fixes their own name and number, and nothing else.

            Open to every approved member -- it is not in ROLE_REQUIRED -- and
            that is the point: your own name is not a privilege. What stops it
            being more than a name is the database, where the "edit own name
            only" policy pins status and role to what they already are.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            try:
                req = self.body(4000)
                if req is None:
                    return
                with db(self.me["id"]) as conn:
                    out = db_edit_profile(conn, self.me["id"],
                                          req.get("name"), req.get("phone"))
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            return self.reply(200, out)

        # ---------------------------------------------------------------
        # The worker API. Four routes, all under WORKER_PREFIX, all reachable
        # only with the shared token -- see the gate above. This is the whole
        # of what a machine outside this one may do: take a lecture off the
        # queue, fetch its audio, and say how it went.

        def lecture(self, lid):
            """(subject_code, title, audio_key, attempts) for one lecture.

            Returns None for anything that is not an id we hold, which is the
            same answer for a malformed uuid and for one that simply is not
            there -- a worker has no business telling those apart.
            """
            try:
                with db() as conn:
                    return conn.execute(
                        "select subject_code, title, audio_key, attempts "
                        "from lectures where id = %s", (lid,)).fetchone()
            except psycopg.Error:
                return None

        def audio_of(self, row):
            return Path(row[2])

        def title_of(self, row):
            """A filename, never a path: this string came out of the database
            and ends up naming a file on disk."""
            raw = row[1] or Path(row[2]).stem
            return re.sub(r"[^A-Za-z0-9._-]", "_", Path(raw).name)[:120] or "lecture"

        def do_worker_claim(self):
            """One queued lecture, or an empty object when there is nothing.

            Atomic because claim_lecture() is: its `for update skip locked`
            means a second worker steps over the row the first is holding
            rather than being handed it twice.
            """
            if not jobs.remote:
                # Two consumers of one queue would transcribe the same lecture
                # twice. If this machine is draining the queue itself, nobody
                # else may.
                return self.reply(503, {"error": "this server runs its own queue; "
                                                 "start it with --remote-workers"})
            try:
                with db() as conn:
                    row = conn.execute(
                        "select id, subject_code, title, audio_key, attempts "
                        "from claim_lecture()").fetchone()
            except psycopg.Error as e:
                return self.reply(503, {"error": f"the library is offline: {e}"})
            if not row:
                return self.reply(200, {})
            lid, code, title, key, attempts = row
            path = Path(key)
            title = self.title_of((code, title, key, attempts))
            if not path.exists():
                # The audio is gone. Fail it here rather than handing a worker
                # a job it cannot begin.
                with db() as conn:
                    conn.execute("update lectures set status = 'failed', error = %s "
                                 "where id = %s",
                                 (f"the audio for {title} is no longer on the server", lid))
                log(f"{path.name} has no audio left; giving up on it", "worker")
                return self.reply(200, {})
            jobs.track(path, code, "transcribing", f"claimed by a worker (try {attempts})")
            log(f"{path.name} claimed by a worker (try {attempts})", "worker")
            return self.reply(200, {"id": str(lid), "subject": code, "title": title,
                                    "name": path.name, "attempts": attempts})

        def do_worker_audio(self):
            import urllib.parse

            lid = urllib.parse.parse_qs(
                self.path.partition("?")[2]).get("id", [""])[0][:64]
            row = self.lecture(lid)
            if not row:
                return self.reply(404, {"error": "no such lecture"})
            path = self.audio_of(row)
            if not path.exists():
                return self.reply(404, {"error": "the audio is gone"})
            size = path.stat().st_size
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.send_header("X-Filename", path.name)
                self.end_headers()
                with path.open("rb") as fh:
                    while chunk := fh.read(262144):
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the worker hung up mid-download; the claim goes stale

        def do_worker_progress(self):
            """What the phone's job list shows while a remote worker works."""
            req = self.body(20000)
            if req is None:
                return
            row = self.lecture(str(req.get("id") or "")[:64])
            if not row:
                return self.reply(404, {"error": "no such lecture"})
            jobs.track(self.audio_of(row), row[0], "transcribing",
                       str(req.get("detail") or "")[:200])
            return self.reply(200, {"ok": True})

        def do_worker_done(self):
            """Transcript and notes back from the worker, stored exactly where
            process() would have put them -- and the audio deleted, because
            once the notes exist it is the biggest thing on the disk and the
            one nobody will ever open again."""
            req = self.body(16 * 1024 * 1024)
            if req is None:
                return
            row = self.lecture(str(req.get("id") or "")[:64])
            if not row:
                return self.reply(404, {"error": "no such lecture"})
            transcript = req.get("transcript") or ""
            notes_md = req.get("notes") or ""
            if not (transcript and notes_md):
                return self.reply(400, {"error": "a transcript and notes are both required"})
            code, path, title = row[0], self.audio_of(row), self.title_of(row)
            out = subject_dir(args.library, code, "lectures") / f"{title}.md"
            out.write_text(lecture_note(title, code, notes_md, transcript))
            cache = Path(args.library) / ".transcripts" / f"{title}.txt"
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(transcript)
            try:
                with db() as conn:
                    conn.execute(
                        "update lectures set status = 'done', error = null, "
                        "transcript = %s, notes_md = %s where id = %s",
                        (transcript, notes_md, req["id"]))
            except psycopg.Error as e:
                # The notes are on disk, which is what the library reads. Say
                # so and let the worker move on rather than making it redo an
                # hour of Whisper over a dropped connection.
                log(f"could not mark {title} done: {e}", "worker")
            path.unlink(missing_ok=True)
            cache.with_suffix(".partial.json").unlink(missing_ok=True)
            jobs.track(path, code, "done", "notes ready")
            log(f"{title} came back from a worker; {path.name} deleted", "worker")
            return self.reply(200, {"ok": True, "notes": out.name})

        def do_worker_failed(self):
            """It went wrong. Back on the queue, unless it has had its three."""
            req = self.body(20000)
            if req is None:
                return
            row = self.lecture(str(req.get("id") or "")[:64])
            if not row:
                return self.reply(404, {"error": "no such lecture"})
            code, path, attempts = row[0], self.audio_of(row), row[3]
            error = str(req.get("error") or "transcription failed")[:500]
            over = attempts >= MAX_ATTEMPTS
            try:
                with db() as conn:
                    conn.execute("update lectures set status = %s, error = %s where id = %s",
                                 ("failed" if over else "queued", error, req["id"]))
            except psycopg.Error as e:
                return self.reply(503, {"error": f"the library is offline: {e}"})
            if over:
                # A file nothing can read may not pin a hundred megabytes of
                # disk forever.
                path.unlink(missing_ok=True)
            jobs.track(path, code, "failed" if over else "queued",
                       error if over else f"{error}, retrying ({attempts} of {MAX_ATTEMPTS})")
            log(f"{path.name}: {error}" + ("" if over else f" (try {attempts}, will retry)"),
                "worker-fail")
            return self.reply(200, {"retrying": not over})

        def do_upload(self):
            """Raw body upload: filename and subject ride in headers.

            Deliberately not multipart -- the cgi module is gone in Python 3.13+
            and a hand-rolled parser is a bug farm for zero benefit here.
            """
            import re
            import urllib.parse
            import uuid as uuidlib

            try:
                n = int(self.headers.get("Content-Length", 0))
                if n <= 0:
                    return self.reply(400, {"error": "empty upload"})

                # Several files, one title: the phone mints one id and sends
                # it on every file in the batch, so this is the one place that
                # id is ever trusted. Anything that does not parse as a uuid
                # is refused outright rather than stored -- a batch_id is
                # never shown to anyone, so a malformed one is not a typo to
                # be forgiving about, it is a client that is not this page.
                batch_raw = self.headers.get("X-Batch", "").strip()
                if batch_raw:
                    try:
                        batch = str(uuidlib.UUID(batch_raw))
                    except ValueError:
                        return self.reply(400, {"error": "bad batch id"})
                else:
                    batch = None
                title = urllib.parse.unquote(
                    self.headers.get("X-Title", "")).strip()[:200] or None

                raw = urllib.parse.unquote(self.headers.get("X-Filename", "upload"))
                # The extension alone, off the raw name, so it survives
                # whichever sanitiser runs next -- neither one touches it.
                is_audio = Path(raw).suffix.lower() in AUDIO_EXTS
                # Never trust a client-supplied filename with a path in it.
                # Documents and photos may keep a space: the whole point of
                # renaming at upload time is a name a person actually typed,
                # and "Unit_3_Notes.pdf" back is not that. Audio keeps the
                # stricter rule -- its filename becomes a lecture's title
                # (dest.stem below), which title_of sanitizes the same strict
                # way again wherever a worker writes it back, so a space
                # allowed only here would not survive there anyway.
                allowed = r"[^A-Za-z0-9._ -]" if not is_audio else r"[^A-Za-z0-9._-]"
                name = re.sub(allowed, "_", Path(raw).name)[:120] or "upload"
                subject = self.headers.get("X-Subject", "").strip()
                code = resolve_subject(subject) if subject else guess_subject(name)
                if not code:
                    return self.reply(400, {"error": f"pick a subject for {name}"})

                # Refused on the declared length, before a byte of the body is
                # read: the point of a limit is not spending ten minutes of
                # somebody's mobile data before saying no. The name and the
                # limit are both in the message, because "too large" leaves a
                # phone with nothing to do about it.
                cap = MAX_AUDIO_BYTES if is_audio else MAX_DOC_BYTES
                if n > cap:
                    self.close_connection = True   # body left unread; do not reuse
                    what = "a recording" if is_audio else "notes and slides"
                    return self.reply(413, {
                        "error": f"{name} is {mb(n, up=True)}, the limit for {what} "
                                 f"is {mb(cap)}", "limit": cap, "size": n})
                dest = (inbox / f"{code}-{name}") if is_audio \
                    else (subject_dir(args.library, code, "uploads") / name)
                if not is_audio:
                    # Only documents and photos: a name typed by hand collides
                    # far more easily than a camera's own filename ever did,
                    # and the inbox is transient in a way the shelf is not.
                    dest = dedupe_path(dest)
                    name = dest.name

                # Stream to a .part file rather than reading the whole body into
                # memory. A phone on wifi sending a 100MB lecture would otherwise
                # buffer all of it here, and a dropped connection would leave a
                # truncated file looking like a real one.
                part = dest.with_suffix(dest.suffix + ".part")
                got = 0
                try:
                    with part.open("wb") as fh:
                        while got < n:
                            chunk = self.rfile.read(min(262144, n - got))
                            if not chunk:
                                raise ConnectionResetError("client stopped sending")
                            fh.write(chunk)
                            got += len(chunk)
                except (ConnectionResetError, BrokenPipeError, OSError) as e:
                    part.unlink(missing_ok=True)
                    log(f"{name} dropped at {got}/{n} bytes ({e})", "upload-fail", 1)
                    return  # socket is gone; replying would only raise again

                part.replace(dest)
                # Who sent it, so the library can say so later. --no-auth has
                # nobody to credit, which is the whole point of --no-auth.
                if self.me:
                    with db(self.me["id"]) as conn:
                        db_record_upload(conn, self.me["id"], code, name, dest,
                                          is_audio, batch=batch, title=title)
                job = jobs.add(dest, code, "audio" if is_audio else "document")
                return self.reply(200, {"job": job})
            except SystemExit as e:
                return self.reply(400, {"error": str(e)})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})

        def do_vote(self):
            """One vote per person per item, and never for your own.

            Three shapes of payload, one route: {"id": ...} is an upload,
            {"answer": ...} is somebody's answer to a doubt and {"post": ...}
            is something said on the wall. They share this
            handler because they share the table, the one-per-person rule and
            the "not your own" rule -- a second endpoint would be a second copy
            of all three, kept in step by hand.

            Both referees are in Postgres: the unique indexes on votes, and the
            "vote as yourself" policy. This handler only puts their refusals
            into words.

            An unauthenticated caller never reaches this: /vote is not in
            PUBLIC_PATHS, so parse_request has already refused them.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            mine = "your own answer"
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n > 4000:
                    self.close_connection = True
                    return self.reply(413, {"error": "too much"})
                req = json.loads(self.rfile.read(n) or b"{}")
                # The request picks between two column names written down in
                # db_vote; it never supplies one.
                target = (req.get("answer") or "").strip()
                col = "doubt_id"
                if not target:
                    target, col, mine = (req.get("post") or "").strip(), \
                        "post_id", "your own post"
                if not target:
                    target, col, mine = (req.get("id") or "").strip(), \
                        "material_id", "your own upload"
                if not target:
                    return self.reply(400, {"error": "which item?"})
                with db(self.me["id"]) as conn:
                    state = db_vote(conn, target, self.me["id"],
                                    bool(req.get("on", True)), col)
            except psycopg.errors.UniqueViolation:
                return self.reply(409, {"error": "you have already voted for this"})
            except psycopg.errors.InsufficientPrivilege:
                # The only insert an approved member's own session can be
                # refused is a vote on something they wrote themselves -- the
                # gate has already turned away everybody else.
                return self.reply(403, {"error": f"you cannot upvote {mine}"})
            except (psycopg.errors.InvalidTextRepresentation,
                    psycopg.errors.ForeignKeyViolation,
                    psycopg.errors.CheckViolation):
                return self.reply(404, {"error": "no such item"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            return self.reply(200, state)

        def thread_asked_for(self, conn, req):
            """(subject code, lecture id, material id) for the thread named.

            One place, because the GET reads it and the POST reads it and a
            question has to land on the thread the reader is looking at.

            The phone names a note the way the library does -- a subject and a
            title -- and the lecture id is looked up here. A title with no row
            behind it is not an error: the thread is then the subject's, which
            is the one every note in it can fall back to.
            """
            import uuid as uuidlib

            # A file is the third kind of thread (0036), and it is the one
            # that names itself: a material row already says which subject it
            # belongs to, so the phone sends an id and nothing else. Asking
            # the phone for the subject as well would be a second fact that
            # can disagree with the first.
            material = (req.get("material") or "").strip()
            if material:
                try:
                    material = str(uuidlib.UUID(material))
                except ValueError:
                    raise ValueError("no such file")
                row = conn.execute(
                    "select subject_code from materials where id = %s", (material,),
                ).fetchone()
                if not row:
                    raise ValueError("no such file")
                return row[0], None, material
            code = (req.get("subject") or "").strip()[:32]
            if code not in SUBJECTS:
                raise ValueError("which subject?")
            return code, db_lecture_id(conn, code,
                                       (req.get("title") or "").strip()[:200]), None

        def do_doubts_get(self):
            """One thread, fetched when the note is opened.

            Its own request rather than a passenger on /data, unlike the notice
            board: the board is thirty lines for the whole class, and this
            would be every question on every lecture in twelve subjects, sent
            to a phone that is going to read one of them.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            import urllib.parse

            try:
                q = urllib.parse.parse_qs(self.path.partition("?")[2])
                with db(self.me["id"]) as conn:
                    code, lecture, material = self.thread_asked_for(
                        conn, {k: v[0] for k, v in q.items()})
                    thread = db_doubts(conn, self.me["id"], code, lecture, material)
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            return self.reply(200, {"doubts": thread})

        def do_bookmark(self):
            """Save a note, or take it back. Nobody else's business.

            Not in ROLE_REQUIRED, same as attendance: a bookmark is a private
            note about what YOU want to find again, not a privilege the class
            grants. Whose it is comes from the session and never the request.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            try:
                req = self.body(2000)
                if req is None:
                    return
                code = (req.get("subject") or "").strip()[:32]
                title = (req.get("title") or "").strip()[:200]
                with db(self.me["id"]) as conn:
                    db_set_bookmark(conn, self.me["id"], code, title,
                                     bool(req.get("on")))
                    saved = db_bookmarks(conn, self.me["id"])
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except psycopg.errors.InsufficientPrivilege:
                return self.reply(403, {"error": "you can only save your own bookmarks"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            return self.reply(200, {"bookmarks": saved})

        def do_doubts(self):
            """Ask, answer, or take back something you wrote.

            One route for three things because they are one table and one
            thread, and because every one of them ends the same way: the whole
            thread comes back, so the screen redraws from what was stored
            rather than from what was typed.

            Deliberately not in ROLE_REQUIRED. A student may not upload, may
            not record and may not spend the class's API budget -- and can
            answer a classmate at one in the morning, which is the whole point
            of this. What is required is approval, and the gate did that.

            And deliberately nothing here calls anything. A doubt is students
            answering students: the one place this app spends money on an API
            is Explain, once per passage, cached by hash on the master device.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            try:
                req = self.body(8000)
                if req is None:
                    return
                with db(self.me["id"]) as conn:
                    code, lecture, material = self.thread_asked_for(conn, req)
                    if req.get("delete"):
                        db_hide_doubt(conn, (req.get("id") or "").strip())
                    else:
                        db_ask(conn, self.me["id"], code, lecture,
                               (req.get("parent") or "").strip() or None,
                               req.get("body"), material)
                    thread = db_doubts(conn, self.me["id"], code, lecture, material)
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except psycopg.errors.InsufficientPrivilege:
                # The insert policy. An approved member is refused exactly one
                # thing here: answering a question that is not there any more.
                return self.reply(404, {"error": "that question is gone"})
            except psycopg.errors.CheckViolation:
                return self.reply(400, {"error": "a question needs something in it"})
            except (psycopg.errors.InvalidTextRepresentation,
                    psycopg.errors.ForeignKeyViolation):
                return self.reply(404, {"error": "no such question"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            return self.reply(200, {"doubts": thread})

        def do_doubts_ai(self):
            """A machine's shot at a question, for when no classmate is awake.

            Nothing is written. The answer is not a row in doubts, does not
            carry an author, cannot be voted on and is gone when the thread is
            closed again -- which is the point: the table is the class
            answering the class, and a bot with a profile in it would sit on
            top of every thread being more confident than the people this
            feature exists for.

            Deliberately not in ROLE_REQUIRED, unlike /explain. The rung on
            /explain is there because Anthropic bills for it; this is Groq's
            free tier, and the whole argument for doubts being open to students
            is that asking costs the class nothing. What is required is
            approval, same as asking and answering.

            The question is read back out of Postgres rather than taken from
            the request, under the caller's own RLS. That is the entire
            permission check: a question the select policy will not show you is
            a question you cannot ask about either, and no new policy had to be
            written to say so.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            try:
                req = self.body(2000)
                if req is None:
                    return
                with db(self.me["id"]) as conn:
                    row = conn.execute(
                        "select subject_code, body from doubts "
                        "where id = %s and parent_id is null and deleted_at is null",
                        ((req.get("id") or "").strip(),)).fetchone()
                if not row:
                    return self.reply(404, {"error": "that question is gone"})
                code, question = row

                # Same cache as Explain, same reason: a hundred and ten people
                # on one thread must be one call. The subject is in the key
                # because it is in the prompt -- the same sentence is a
                # different question in two different courses.
                key = hashlib.sha256(f"{code}\n{question}".encode()).hexdigest()[:32]
                hit = cache_dir / f"doubt-{key}.md"
                if hit.exists():
                    return self.reply(200, {"text": hit.read_text(), "cached": True})

                if budget["left"] <= 0:
                    return self.reply(429, {"error": "AI limit reached for this session"})
                budget["left"] -= 1

                subject = SUBJECTS.get(code, (code,))[0].replace("-", " ")
                out = groq(DOUBT_PROMPT, f"In {subject}:\n\n{question}", ai_model)
                hit.write_text(out)
                log(f"{budget['left']} left, cached {key[:8]}", "doubt-ai", 1)
                self.reply(200, {"text": out})
            except psycopg.errors.InvalidTextRepresentation:
                return self.reply(404, {"error": "no such question"})
            except Exception as e:
                # The phone is told the real reason, exactly as /explain does:
                # a missing key, a spent daily quota and a dropped connection
                # are three different problems and one message for all three
                # sends everybody to the wrong fix.
                self.reply(500, {"error": f"{type(e).__name__}: {e}"})

        def do_posts_get(self):
            """One wall, fetched when Campus opens it.

            Its own request rather than a passenger on /data: /data is what
            Home waits for, and sixty posts with their photos is not what a
            student opening the app in a corridor is waiting to read.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            import urllib.parse

            try:
                q = urllib.parse.parse_qs(self.path.partition("?")[2])
                kind = (q.get("kind", ["feed"])[0] or "feed").strip()
                if kind not in ("feed", "confession"):
                    return self.reply(400, {"error": "which wall?"})
                with db(self.me["id"]) as conn:
                    wall = db_posts(conn, self.me["id"], kind, args.out.parent)
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            return self.reply(200, {"kind": kind, "posts": wall})

        def do_posts(self):
            """Post to the section, or take one down. Either way the wall
            comes back, so the screen redraws from what was stored.

            Deliberately not in ROLE_REQUIRED: any approved member posts. What
            IS gated, and gated in Postgres, is how many confessions one
            person gets in a day and whether anybody may learn who wrote one.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            try:
                req = self.body(8000)
                if req is None:
                    return
                kind = (req.get("kind") or "feed").strip()
                if kind not in ("feed", "confession"):
                    return self.reply(400, {"error": "which wall?"})
                with db(self.me["id"]) as conn:
                    if req.get("delete"):
                        db_hide_post(conn, (req.get("id") or "").strip())
                    else:
                        db_post(conn, self.me["id"], kind, req.get("body"),
                                (req.get("batch") or "").strip() or None)
                    wall = db_posts(conn, self.me["id"], kind, args.out.parent)
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except psycopg.errors.RaiseException as e:
                # The daily limit, raised by the trigger in 0035. 429 rather
                # than 400: nothing about the post was wrong, it was the
                # fourth one today.
                return self.reply(429, {"error": e.diag.message_primary
                                               or "that is enough for today"})
            except psycopg.errors.InsufficientPrivilege:
                return self.reply(403, {"error": "you cannot post as somebody else"})
            except psycopg.errors.CheckViolation:
                return self.reply(400, {"error": "a post needs something in it"})
            except (psycopg.errors.InvalidTextRepresentation,
                    psycopg.errors.ForeignKeyViolation):
                return self.reply(404, {"error": "no such post"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            return self.reply(200, {"kind": kind, "posts": wall})

        def do_confession_author(self):
            """Who wrote one confession. An admin, deliberately, off the wall.

            /confession-author is in ROLE_REQUIRED and confession_author() in
            Postgres asks is_admin() again on the caller's own connection, so
            a member who gets past the first -- a route added wrong, a session
            promoted and demoted, curl -- is answered "no such confession" by
            the database rather than by this file's opinion.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            import urllib.parse

            try:
                q = urllib.parse.parse_qs(self.path.partition("?")[2])
                with db(self.me["id"]) as conn:
                    who = db_confession_author(conn, (q.get("id", [""])[0]).strip())
            except ValueError as e:
                return self.reply(404, {"error": str(e)})
            except psycopg.errors.InvalidTextRepresentation:
                return self.reply(404, {"error": "no such confession"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            return self.reply(200, {"by": who})

        def do_chat_get(self):
            """A room, or only what is new in it.

            `since` is what keeps an open room cheap: the phone sends the last
            id it holds and gets what came after it, which is usually nothing.
            Without it this is the first look at the room and the tail comes
            back. Either way the whole history is never sent twice.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            import urllib.parse

            try:
                q = urllib.parse.parse_qs(self.path.partition("?")[2])
                code = (q.get("subject", [""])[0]).strip()[:32]
                if code not in SUBJECTS:
                    return self.reply(400, {"error": "which subject?"})
                try:
                    since = int(q.get("since", ["0"])[0] or 0)
                except ValueError:
                    since = 0
                with db(self.me["id"]) as conn:
                    said = db_chat(conn, self.me["id"], code, since)
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            return self.reply(200, {"subject": code, "messages": said,
                                    "since": since})

        def do_chat(self):
            """Say something in a room, or take a message down.

            The reply is only what is new to the caller -- the same `since`
            the poll uses -- so posting costs the room's tail once and never
            again. Deliberately not in ROLE_REQUIRED: a room the class cannot
            talk in is a room.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            try:
                req = self.body(4000)
                if req is None:
                    return
                code = (req.get("subject") or "").strip()[:32]
                if code not in SUBJECTS:
                    return self.reply(400, {"error": "which subject?"})
                try:
                    since = int(req.get("since") or 0)
                except (TypeError, ValueError):
                    since = 0
                with db(self.me["id"]) as conn:
                    if req.get("delete"):
                        db_hide_message(conn, int(req.get("id") or 0))
                        # A removed message is not "after" anything, so the
                        # phone is told which id to drop rather than being
                        # made to refetch the room to discover it is gone.
                        said = db_chat(conn, self.me["id"], code, since)
                        return self.reply(200, {"subject": code, "messages": said,
                                                "removed": int(req.get("id") or 0)})
                    db_say(conn, self.me["id"], code, req.get("body"))
                    said = db_chat(conn, self.me["id"], code, since)
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except psycopg.errors.RaiseException as e:
                return self.reply(429, {"error": e.diag.message_primary
                                               or "slow down a moment"})
            except psycopg.errors.InsufficientPrivilege:
                return self.reply(403, {"error": "you cannot talk as somebody else"})
            except psycopg.errors.CheckViolation:
                return self.reply(400, {"error": "type something first"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            return self.reply(200, {"subject": code, "messages": said})

        def do_attendance(self):
            """Mark one class, or a day of them. Your own, and only your own.

            Deliberately not in ROLE_REQUIRED: a mark is a private note a
            student makes about themselves, not a privilege. Whose it is comes
            from the session and never from the request, and the policy pins it
            there again -- so the two ways to get this wrong, a forged
            profile_id and a forgotten check, are both already shut.

            The answer carries the whole attendance payload back, not just
            "ok". Marking changes the percentage, the consequence sentence and
            possibly the subject's whole standing, and the phone repainting
            those from its own arithmetic is a second place for the maths to
            live and disagree.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            try:
                req = self.body(20000)
                if req is None:
                    return
                marks = req.get("marks")
                if not isinstance(marks, list):
                    return self.reply(400, {"error": "expected a list of marks"})
                with db(self.me["id"]) as conn:
                    db_mark_attendance(conn, self.me["id"], marks)
                    return self.reply(200, db_attendance(conn, self.me["id"]))
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except psycopg.errors.InsufficientPrivilege:
                # The policy refused it: not approved, or a row that is not
                # theirs. Either way the app has no screen for it.
                return self.reply(403, {"error": "you can only mark your own attendance"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})

        def do_cancelled(self):
            """Call one class off for the whole section, or put it back.

            Trusted, and gated twice: the gate refused everybody else before
            this was reached, and the write runs on the caller's own connection
            where the policy has to allow it too.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            try:
                req = self.body(4000)
                if req is None:
                    return
                with db(self.me["id"]) as conn:
                    db_set_cancelled(conn, self.me["id"], req.get("date"),
                                     req.get("period"), req.get("code"),
                                     bool(req.get("off")), req.get("reason"))
                    out = db_attendance(conn, self.me["id"])
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except psycopg.errors.InsufficientPrivilege:
                return self.reply(403, {"error": "calling a class off is for "
                                                 "trusted members", "required": "trusted"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            return self.reply(200, out)

        def do_announce(self):
            """Post, edit, hide or restore one notice. Class reps and above.

            Gated twice over, like every other route that writes: the gate
            refused everybody below cr before this was reached, and the write
            runs on the caller's own connection where "class reps edit their
            own announcements" has to allow it too -- which is what makes
            "their own" true rather than intended.

            The rung is asked for by name rather than by role equality, so the
            day an admin is not the only person above a cr this still means
            what it says.
            """
            if not self.at_least("cr"):
                return self.reply(403, {"error": "the notice board is for class "
                                                 "representatives", "required": "cr"})
            try:
                req = self.body(20000)
                if req is None:
                    return
                with db(self.me["id"]) as conn:
                    aid = db_write_announcement(
                        conn, self.me["id"], (req.get("id") or "").strip() or None,
                        req.get("title"), req.get("body"), req.get("pinned"),
                        req.get("deleted"))
                    board = db_announcements(conn, self.me["id"])
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except psycopg.errors.CheckViolation:
                return self.reply(400, {"error": "an announcement needs a title"})
            except psycopg.errors.InvalidTextRepresentation:
                return self.reply(404, {"error": "no such announcement"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            log(f"{self.me['name']} posted or changed announcement {aid}", "admin")
            return self.reply(200, {"id": aid, "announcements": board})

        def do_campus(self, kind):
            """Write one club, one event or one place, and hand the tab back.

            One handler for three tables because it is one shape of request:
            an admin or a trusted member changes a row, and the screen redraws
            from what was stored rather than from what was typed. Three
            handlers would be three copies of the same six error branches.

            What separates them is not here. ROLE_REQUIRED refused the wrong
            role before this ran, and the policies refuse again on the caller's
            own connection -- so "admins only" is true of clubs and places even
            for somebody holding a stolen cookie and curl.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            try:
                req = self.body(8000)
                if req is None:
                    return
                with db(self.me["id"]) as conn:
                    if kind == "club":
                        db_write_club(conn, (req.get("slug") or "").strip() or None,
                                      req.get("name"), req.get("blurb"),
                                      req.get("category"), req.get("tags"),
                                      req.get("link"), req.get("contact"),
                                      req.get("hidden"))
                    elif kind == "event":
                        db_write_event(conn, self.me["id"],
                                       (req.get("id") or "").strip() or None,
                                       req.get("title"), req.get("society"),
                                       req.get("date"), req.get("ends"),
                                       req.get("venue"), req.get("blurb"),
                                       req.get("deleted"))
                    elif req.get("remove"):
                        db_remove_place(conn, (req.get("slug") or "").strip())
                    else:
                        db_write_place(conn, (req.get("slug") or "").strip() or None,
                                       req.get("name"), req.get("kind"),
                                       req.get("lat"), req.get("lng"),
                                       req.get("approx"), req.get("note"))
                    out = {"clubs": db_clubs(conn), "events": db_events(conn, self.me["id"]),
                           "places": db_places(conn), "maps": maps_config()}
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except psycopg.errors.InsufficientPrivilege:
                needed = "trusted" if kind == "event" else "admin"
                return self.reply(403, {"error": f"changing a {kind} is for "
                                                 f"{needed} members",
                                        "required": needed})
            except psycopg.errors.CheckViolation:
                return self.reply(400, {"error": f"that is not a usable {kind}"})
            except (psycopg.errors.InvalidTextRepresentation,
                    psycopg.errors.ForeignKeyViolation):
                return self.reply(404, {"error": f"no such {kind}"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            return self.reply(200, out)

        def do_read(self):
            """"I have seen these." Every approved member, about themselves.

            Deliberately not in ROLE_REQUIRED: what you have read is not a
            privilege, and the insert policy pins the row to the person asking
            whatever the request says.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            try:
                req = self.body(4000)
                if req is None:
                    return
                ids = req.get("ids")
                if not isinstance(ids, list):
                    return self.reply(400, {"error": "expected a list of ids"})
                with db(self.me["id"]) as conn:
                    db_mark_read(conn, self.me["id"], ids)
            except psycopg.errors.InvalidTextRepresentation:
                return self.reply(400, {"error": "that is not an announcement id"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            return self.reply(200, {"ok": True})

        def do_revise(self):
            import json as _json
            from argparse import Namespace

            try:
                n = int(self.headers.get("Content-Length", 0))
                req = _json.loads(self.rfile.read(n) or b"{}")
                code = resolve_subject(req.get("subject", ""))
                revise(Namespace(library=args.library, subject=code,
                                 notes_model=args.notes_model, max_cost=args.max_cost))
                return self.reply(200, {"ok": True})
            except SystemExit as e:
                return self.reply(400, {"error": str(e)})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})

        def body(self, cap):
            """The request's JSON body, or None once a 413 has been sent.

            Answers the oversized case itself, so the three admin POSTs below
            read as three lines rather than three copies of the same guard --
            and none of them can be the one that forgets the cap.
            """
            n = int(self.headers.get("Content-Length", 0))
            if n > cap:
                self.close_connection = True   # body left unread; do not reuse
                self.reply(413, {"error": "too much"})
                return None
            return json.loads(self.rfile.read(n) or b"{}")

        def send_html(self, html):
            body = html.encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def send_js(self, js):
            # no-store, same as send_html: a service worker script the browser
            # kept an old copy of is a phone that never learns the cache
            # strategy changed, and the update check itself needs the request
            # to actually reach the server.
            body = js.encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def reply(self, code, obj, cookie=None):
            body = json.dumps(obj).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # phone hung up; nothing useful left to do

    class Server(ThreadingHTTPServer):
        # The stdlib listens five deep. A reel puts a hundred phones on the
        # door inside a minute, and the sixth one through does not get a slow
        # answer -- its SYN is dropped, and the phone shows a page that failed
        # to load. This is the kernel's waiting room, not a thread pool: it
        # costs nothing to make it big enough for the whole class.
        request_queue_size = 128

    return Server((args.host, args.port), partial(Handler, directory=str(root)))


def serve(args):
    """Serve the library and answer 'explain this' taps from the phone.

    The API key never leaves the Mac: the phone posts the highlighted text here
    and gets prose back.
    """
    # Two keys now, and only one of them is about serve(). Anthropic is what
    # turns a recording into notes, on this machine, in the worker thread;
    # Groq is what the phone reaches, and it is the free one.
    if not os.environ.get("GROQ_API_KEY"):
        print("no GROQ_API_KEY set - Explain and Ask AI will return an error",
              file=sys.stderr)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("no ANTHROPIC_API_KEY set - uploads cannot become notes", file=sys.stderr)

    srv = build_server(args)
    ip = lan_ip() if args.host == "0.0.0.0" else args.host   # print what it bound to
    log(f"http://{ip}:{args.port}   <- open this on your phone", "ready")
    log(f"library {args.library}", "ready")
    log(f"log file {LOG_PATH}", "ready")
    if args.no_auth:
        log("running with --no-auth: no join screen, no gate, anyone can read", "ready")
    else:
        log(f"http://{ip}:{args.port}/admin   <- approve joiners here", "ready")
    if args.host == "0.0.0.0":
        print("  reachable by anyone on this wifi; Explain is capped at "
              f"{args.max_explains} calls this run. Ctrl-C to stop.\n", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)


class Unreachable(Exception):
    """The server could not be talked to. Says nothing about the lecture.

    The distinction is the whole point: a lecture that failed has earned one of
    its three attempts, and the third deletes the recording. A wifi blip has
    earned nothing, so it must never be reported as a failure.
    """


def worker(args):
    """Take lectures off a recarve server, transcribe them here, send them back.

        ./notes.py worker --server https://notes.workwithani.tech

    This is the half of the split that needs a GPU. The server hosts the files
    and the site and keeps running when this Mac is shut; this loop is the only
    thing that ever loads Whisper, and it can disappear for a day without the
    class noticing anything but a queue that has stopped moving.

    Nothing here is stateful. A killed worker loses the claim it was holding
    and claim_lecture() hands the lecture out again two hours later; the audio
    and the checkpoint are still in --workdir, so that retry resumes where this
    run stopped rather than starting the hour over.
    """
    import shutil
    import urllib.error
    import urllib.request

    token = args.token or worker_token()
    base = args.server.rstrip("/")
    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)

    def call(path, body=None):
        req = urllib.request.Request(
            f"{base}{path}", method="POST" if body is not None else "GET",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-Worker-Token": token,
                     **({"Content-Type": "application/json"} if body is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError:
            raise                    # the server answered; that is not the uplink
        except OSError as e:
            raise Unreachable(f"{type(e).__name__}: {e}") from e

    def sweep(job):
        """Everything this lecture left behind. Named for the id, so one glob
        catches the audio, the checkpoint, a half-download and the notes."""
        for leftover in work.glob(f"{job['id']}-*"):
            leftover.unlink(missing_ok=True)

    def download(job, dest):
        """The audio, straight to disk. An hour of lecture does not go through
        memory on the way past."""
        if dest.exists():
            log(f"{job['name']} is already here, reusing it", "worker", 1)
            return dest
        req = urllib.request.Request(f"{base}/worker/audio?id={job['id']}",
                                     headers={"X-Worker-Token": token})
        part = Path(f"{dest}.part")
        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as r, part.open("wb") as fh:
                shutil.copyfileobj(r, fh, 262144)
        except urllib.error.HTTPError:
            raise
        except OSError as e:
            part.unlink(missing_ok=True)
            raise Unreachable(f"{type(e).__name__}: {e}") from e
        part.replace(dest)
        log(f"{job['name']}  {mb(dest.stat().st_size)}", "download", 1)
        return dest

    def send_back(finished):
        """The one post worth retrying by hand: an hour of Whisper and a notes
        call already paid for must not die with the uplink."""
        for delay in (0, args.poll, args.poll * 2, args.poll * 4):
            time.sleep(delay)
            try:
                return call("/worker/done", json.loads(finished.read_text()))
            except Unreachable as e:
                last = e
                log(f"could not send it back ({e}); trying again", "worker", 1)
        raise last

    def run(job):
        # Keyed on the lecture id, never on the filename: two phones both call
        # it "New Recording 1.m4a", and a stale file from a lecture that failed
        # weeks ago must not be mistaken for this one's audio.
        audio = work / f"{job['id']}-{job['name']}"
        checkpoint = Path(f"{audio}.partial.json")
        finished = Path(f"{audio}.done.json")
        if finished.exists():
            log(f"{job['title']} was already transcribed here; sending it back",
                "worker", 1)
            send_back(finished)
            sweep(job)
            return
        download(job, audio)
        t0 = time.time()

        def progress(done_sec, total_sec, lang):
            pct = int(done_sec / max(total_sec, 1) * 100)
            eta = (time.time() - t0) / max(done_sec, 1) * (total_sec - done_sec)
            try:
                call("/worker/progress", {
                    "id": job["id"],
                    "detail": f"{pct}% · {int(done_sec) // 60} of "
                              f"{int(total_sec) // 60} min · ~{hhmm(eta)} left"})
            except Exception:
                pass  # the phone's percentage is a nicety; the lecture is not

        segments, languages = transcribe(audio, args.model, args.lang,
                                         checkpoint=checkpoint, on_progress=progress)
        if not segments:
            raise ValueError("no speech in this recording")
        transcript = format_transcript(drop_repeats(segments))
        langs = ", ".join(f"{l}x{languages.count(l)}" for l in sorted(set(languages)))
        log(f"{segments[-1][0] / 60:.0f} min in {hhmm(time.time() - t0)}  [{langs}]",
            "done", 1)

        rate_in, rate_out = price_of(args.notes_model)
        worst = len(transcript) / 4 / 1e6 * rate_in + 16000 / 1e6 * rate_out
        if worst > args.max_cost:
            raise ValueError(f"notes would cost up to ${worst:.2f}, over "
                             f"--max-cost ${args.max_cost:.2f}")
        try:
            call("/worker/progress", {"id": job["id"], "detail": "writing the notes"})
        except Exception:
            pass
        notes, usage = make_notes(transcript, args.notes_lang, args.notes_model)
        cost = usage.input_tokens / 1e6 * rate_in + usage.output_tokens / 1e6 * rate_out
        log(f"{usage.input_tokens} in / {usage.output_tokens} out  ~${cost:.4f}  "
            f"{args.notes_model}", "notes", 1)

        # On disk before it is sent, so the uplink dying now costs a post and
        # not the lecture.
        finished.write_text(json.dumps(
            {"id": job["id"], "transcript": transcript, "notes": notes}))
        send_back(finished)
        sweep(job)
        log(f"{job['title']} sent back", "worker", 1)

    log(f"worker on {base}, files in {work}", "ready")
    wait = args.poll
    while True:
        try:
            job = call("/worker/claim", {})
        except Exception as e:
            # The server restarting, the wifi gone, the tunnel down. None of
            # these is worth a restart by hand, so it is a longer sleep and
            # another try -- the same path an empty queue takes.
            log(f"{base} unreachable ({e}); trying again in {hhmm(wait)}", "worker")
            time.sleep(wait)
            wait = min(wait * 2, args.max_poll)
            continue
        if not job:
            time.sleep(wait)
            wait = min(wait * 2, args.max_poll)   # an idle queue is not hammered
            continue
        wait = args.poll                          # work means work is likely
        log(f"{job['title']} ({job['subject']}), try {job['attempts']}", "claimed")
        try:
            run(job)
        except KeyboardInterrupt:
            raise
        except Unreachable as e:
            # Not the lecture's fault, so it may not be charged an attempt --
            # the third one deletes the recording. Say nothing and let the
            # claim go stale; the transcript and notes wait in the workdir and
            # the next claim of this lecture just posts them.
            log(f"{job['title']}: {e}; leaving the claim to go stale", "worker")
            time.sleep(wait)
            wait = min(wait * 2, args.max_poll)
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            log(f"{job['title']}: {reason}", "worker-fail")
            try:
                if not call("/worker/failed",
                            {"id": job["id"], "error": reason}).get("retrying"):
                    sweep(job)   # nothing will ever claim it again
            except Exception as e2:
                # Unreported, so the claim simply goes stale and the queue
                # hands it out again. Nothing is lost either way.
                log(f"could not report that failure: {e2}", "worker")


def lan_ip():
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # never sends a packet; just picks the route
        return s.getsockname()[0]
    except Exception:
        return "localhost"
    finally:
        s.close()


def list_subjects(args):
    for code, (name, _) in SUBJECTS.items():
        folder = Path(args.library) / f"{code}-{name}"
        lectures = len(list((folder / "lectures").glob("*.md"))) if folder.exists() else 0
        uploads = len(list((folder / "uploads").iterdir())) if (folder / "uploads").exists() else 0
        print(f"  {code}  {name:<34} {lectures:>3} lectures  {uploads:>3} uploads")


def import_timetable(args):
    """Replace a section's weekly template from a CSV. All of it, or none of it.

    The template is a hundred and ten people's Monday, so this prints what
    would change before it changes anything, and refuses to write a single row
    while any line is wrong -- a half-applied grid is the one state nobody can
    look at and tell is wrong.

    It writes the template and then puts it on every member of that section,
    because nothing else can: a student cannot edit their own week, so a
    correction that stopped at the template would be one nobody ever sees. The
    trigger on profiles seeds whoever joins after this.
    """
    text = sys.stdin.read() if str(args.csv) == "-" else args.csv.read_text(encoding="utf-8")
    conn = db()
    known = [code for (code,) in conn.execute("select code from subjects")]
    rows, errors = parse_timetable(text, known)
    for e in errors:
        print(f"  {e}", file=sys.stderr)
    if errors:
        raise SystemExit(f"{len(errors)} problem{'' if len(errors) == 1 else 's'}, "
                         "so nothing was written")

    # Sections arrive in migrations 0040-0044 and this command is older than
    # they are. Ask the database which world it is in rather than guessing:
    # writing every section's Monday because a column was missing is exactly
    # the silent damage the parser above refuses to do.
    scoped = conn.execute(
        "select 1 from information_schema.columns where table_name = 'section_timetable' "
        "and column_name = 'section_id'").fetchone() is not None
    if scoped:
        if not args.section:
            raise SystemExit("this database has sections: name one, e.g. --section I")
        found = conn.execute(
            "select id from sections where name = %s and (%s::int is null or grad_year = %s)",
            (args.section, args.grad_year, args.grad_year)).fetchall()
        if len(found) != 1:
            raise SystemExit(f"{len(found)} sections named {args.section!r}"
                             f"{'' if args.grad_year is None else f' of {args.grad_year}'}"
                             " -- name one exactly, with --grad-year if you must")
        params, where = (found[0][0],), " where section_id = %s"
    elif args.section:
        raise SystemExit("this database has no sections yet, so --section means nothing")
    else:
        params, where = (), ""

    before = {(day, period): code for day, period, code in conn.execute(
        "select day, period, subject_code from section_timetable" + where, params)}
    after = {(day, period): code for day, period, code in rows}
    changed = 0
    for slot in sorted(set(before) | set(after)):
        was, now = before.get(slot), after.get(slot)
        if was != now:
            changed += 1
            print(f"  {DAYS[slot[0]]:<9} period {slot[1]}  {was or '--':<7} -> {now or '--'}")
    print(f"{len(rows)} periods over {len({day for day, _, _ in rows})} days, "
          f"{changed} slot{'' if changed == 1 else 's'} changed")
    if args.dry_run:
        print("--dry-run, so nothing was written")
        return

    cols = "day, period, subject_code" + (", section_id" if scoped else "")
    marks = "%s, %s, %s" + (", %s" if scoped else "")
    with conn.transaction():
        conn.execute("delete from section_timetable" + where, params)
        for day, period, code in rows:
            conn.execute(f"insert into section_timetable ({cols}) values ({marks})",
                         (day, period, code) + params)
        # Only where there are sections to scope it to. A database old enough
        # to have no section_id has no my_section() either, and rewriting every
        # student's week on a guess is the silent damage this command refuses
        # to do everywhere else.
        people = db_reseed_section_weeks(conn, params[0]) if scoped else None
    print(f"written: {len(rows)} periods"
          + ("" if people is None else
             f", onto {people} week{'' if people == 1 else 's'}"))


def process(path, args):
    log(path.name, "file")
    outdir, code = destination(path, args, "lectures")
    out = outdir / f"{path.stem}.md"

    # Never pay for the same lecture twice.
    if out.exists() and not (args.force or args.redo_notes):
        log(f"already done: {out.name} (--force to redo)", "skip", 1)
        return

    # Transcribing is free but slow; caching it means a failed or re-run notes
    # step never costs another 11 minutes of Whisper.
    cache = Path(args.library) / ".transcripts" / f"{path.stem}.txt"
    partial = cache.with_suffix(".partial.json")
    if cache.exists() and not args.force:
        transcript = cache.read_text()
        log(f"reusing cached transcript, {len(transcript.splitlines())} lines", "cache", 1)
    else:
        if args.force and partial.exists():
            partial.unlink()
        t0 = time.time()
        segments, languages = transcribe(path, args.model, args.lang, checkpoint=partial,
                                         on_progress=getattr(args, "on_progress", None))
        if not segments:
            log("no speech found, skipping", "warn", 1)
            return
        kept = drop_repeats(segments)
        transcript = format_transcript(kept)
        mins = segments[-1][0] / 60
        dropped = len(segments) - len(kept)
        note = f", dropped {dropped} repeated" if dropped else ""
        langs = ", ".join(f"{l}x{languages.count(l)}" for l in sorted(set(languages)))
        log(f"{mins:.0f} min of audio in {hhmm(time.time() - t0)}  [{langs}]{note}",
            "done", 1)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(transcript)
        partial.unlink(missing_ok=True)  # full transcript supersedes the checkpoint
    if args.no_notes:
        body = (f"# {lecture_title(path.stem, code)}\n\n"
                f"## Transcript\n\n```\n{transcript}\n```\n")
    else:
        # ~4 chars per token, plus the 16k output ceiling, at this model's rates.
        rate_in, rate_out = price_of(args.notes_model)
        worst_case = len(transcript) / 4 / 1e6 * rate_in + 16000 / 1e6 * rate_out
        if worst_case > args.max_cost:
            raise SystemExit(
                f"  would cost up to ${worst_case:.2f}, over --max-cost ${args.max_cost:.2f}.\n"
                f"  Transcript is cached, so raise --max-cost and re-run without re-transcribing."
            )
        context = []
        if code and args.context:
            context, included, skipped = subject_context(args.library, code)
            if included:
                log(f"context: {len(included)} file(s) - {', '.join(included)}", "context", 1)
            if skipped:
                log(f"SKIPPED over {MAX_CONTEXT_BYTES // 1024 // 1024}MB: "
                    f"{', '.join(skipped)}", "warn", 1)

        notes, usage = make_notes(transcript, args.notes_lang, args.notes_model, context)
        cost = usage.input_tokens / 1e6 * rate_in + usage.output_tokens / 1e6 * rate_out
        log(f"{usage.input_tokens} in / {usage.output_tokens} out  ~${cost:.4f}  "
            f"{args.notes_model}", "notes", 1)
        body = lecture_note(path.stem, code, notes, transcript)

    out.write_text(body)
    print(f"  -> {out}", file=sys.stderr)


def selftest():
    """Chunk advance must follow Whisper's segments and drop truncated tails."""
    import types

    calls = []

    def fake_transcribe(window, **kw):
        calls.append(len(window))
        # Two clean segments then a cut-off one, mirroring a real chunk.
        return {
            "language": "hi" if len(calls) % 2 else "en",
            "segments": [
                {"start": 0.0, "end": 50.0, "text": f"seg{len(calls)}a"},
                {"start": 50.0, "end": 100.0, "text": f"seg{len(calls)}b"},
                {"start": 100.0, "end": 120.0, "text": "truncated"},
            ],
        }

    import numpy as np

    fake = types.SimpleNamespace(
        transcribe=fake_transcribe,
        audio=types.SimpleNamespace(load_audio=lambda p, sr: np.zeros(300 * SAMPLE_RATE)),
    )
    sys.modules["mlx_whisper"] = fake
    sys.modules["mlx_whisper.audio"] = fake.audio

    segments, languages = transcribe("fake.wav", "large-v3", None, verbose=False)
    texts = [t for _, t in segments]

    # Every chunk drops its cut-off tail except the last, where nothing follows it.
    assert texts.count("truncated") == 1, f"tail dropped wrong: {texts}"
    assert texts[-1] == "truncated", f"final chunk must keep its tail: {texts}"
    # 300s of audio advancing 100s per chunk -> 2 kept + 2 kept + 3 kept.
    assert len(segments) == 7, f"expected 7 segments, got {len(segments)}"
    # Second chunk starts at 100s, so its first segment sits at 100s absolute.
    assert segments[2][0] == 100.0, f"chunk 2 offset wrong: {segments[2][0]}"
    assert languages == ["hi", "en", "hi"], f"per-chunk language lost: {languages}"

    assert format_transcript([(0.0, "hi"), (65.0, "there")]) == "[00:00] hi\n[01:05] there"

    # Resume: a checkpoint written mid-lecture must skip the audio already done.
    import json, tempfile

    tmp = Path(tempfile.mkdtemp())
    ck = tmp / "ck.json"
    ck.write_text(json.dumps({"pos": 100 * SAMPLE_RATE, "segments": [[0.0, "earlier"]], "languages": ["hi"]}))
    calls.clear()
    resumed, langs2 = transcribe("fake.wav", "large-v3", None, verbose=False, checkpoint=ck)
    assert resumed[0] == (0.0, "earlier"), "prior segments must survive a resume"
    assert len(calls) == 2, f"resumed run should transcribe 2 chunks, not {len(calls)}"
    assert langs2[0] == "hi", "prior languages must survive a resume"
    assert json.loads(ck.read_text())["pos"] > 100 * SAMPLE_RATE, "checkpoint must advance"

    # Folder expansion picks up audio and ignores everything else.
    (tmp / "a.m4a").touch(); (tmp / "b.mp4").touch(); (tmp / "notes.pdf").touch()
    got = {f.name for f in collect_audio([tmp])}
    assert got == {"a.m4a", "b.mp4"}, f"folder expansion wrong: {got}"

    # Subject context: only the right extensions, and the byte cap is honoured.
    lib = tmp / "lib"
    up = lib / "CY1107-Engineering-Chemistry" / "uploads"
    up.mkdir(parents=True)
    (up / "slides.pdf").write_bytes(b"%PDF-1.4 fake")
    (up / "notes.md").write_text("prof notes")
    (up / "photo.jpg").write_bytes(b"\xff\xd8")
    blocks, included, skipped = subject_context(lib, "CY1107")
    assert set(included) == {"slides.pdf", "notes.md"}, included
    assert not skipped and len(blocks) == 2
    assert blocks[0]["source"]["type"] in ("base64", "text")
    # A budget smaller than the files must skip them BY NAME, never silently.
    _, inc2, skip2 = subject_context(lib, "CY1107", limit_bytes=5)
    assert inc2 == [] and set(skip2) == {"slides.pdf", "notes.md"}, (inc2, skip2)
    # A subject with no uploads folder is fine, not an error.
    assert subject_context(lib, "MC1101") == ([], [], [])

    # Repetition filter: kill hallucinations, keep genuine repetition.
    assert drop_repeats([(0.0, "hello"), (5.0, "hello")]) == [(0.0, "hello")], "consecutive dupe"
    assert drop_repeats([(0.0, "प्रस्तुति प्रस्तुति")]) == [], "single word repeated is noise"
    # The professor saying the same sentence again after other content is real.
    spaced = [(0.0, "note this"), (5.0, "other"), (9.0, "note this")]
    assert drop_repeats(spaced) == spaced, "non-consecutive repeats must survive"
    assert drop_repeats([(0.0, "very very good")]) == [(0.0, "very very good")], "not all-same"

    # Subject resolution: codes, aliases, and case/separator tolerance.
    assert resolve_subject("CY1107") == "CY1107"
    assert resolve_subject("chem") == "CY1107"
    assert resolve_subject(" Maths ") == "MC1101"
    assert resolve_subject("env_sci") == "CY1110"

    # Filename inference: longest alias wins, so the lab beats the lecture course.
    assert guess_subject("2026-09-08 chem-lab batch1.pdf") == "CY1126"
    assert guess_subject("chemistry notes.pdf") == "CY1107"
    assert guess_subject("iks-week3.pdf") == "HS1112"
    assert guess_subject("lecture.m4a") is None, "unmatched filename must not be filed"
    # A longer alias settles a filename that mentions two subjects.
    assert guess_subject("bio-and-math.pdf") == "MC1101"
    # 'bio' and 'iks' are both 3 chars, so nothing breaks the tie -> refuse to guess.
    assert guess_subject("bio-iks-combined.pdf") is None, "ambiguous filename must not be filed"

    # ---- Practice mode reads the questions the notes already carry. ----
    note = (
        "# Maths\n\n## Summary\n\nlimits.\n\n## Questions\n\n"
        "**1. What is the limit definition of $f'(x)$?**\n\n"
        "<details><summary>Answer</summary>\n\n"
        "$$f'(x) = \\lim_{h \\to 0} \\frac{f(x+h)-f(x)}{h}$$\n\n</details>\n\n"
        "### Q2. State the power rule\n\n"                     # question as a heading
        "<details><summary>Answer</summary>\n\n$nx^{n-1}$\n\n</details>\n\n"
        "**Q3: Derive it from first principles.**\n"           # no blank line, colon
        "<details><summary>Answer</summary>\n\nExpand and cancel.\n\n</details>\n\n"
        "---\n\n<details><summary>Full transcript</summary>\n\n```\n[00:00] hi\n```\n\n</details>\n"
    )
    qs = parse_questions(note)
    assert len(qs) == 3, f"expected 3 questions, got {[q['q'] for q in qs]}"
    assert qs[0]["q"] == "What is the limit definition of $f'(x)$?", qs[0]["q"]
    assert qs[1]["q"] == "State the power rule", qs[1]["q"]
    assert qs[2]["q"] == "Derive it from first principles.", qs[2]["q"]
    assert "\\lim" in qs[0]["a"], qs[0]["a"]
    # Every lecture ends in a <details> holding the transcript. It is not a question.
    assert all("[00:00]" not in q["a"] for q in qs), "the transcript leaked into the quiz"

    # Malformed notes offer no practice at all rather than an empty quiz.
    assert parse_questions("") == []
    assert parse_questions("## Questions\n\n1. Where is the answer block?\n") == []
    assert parse_questions("## Questions\n\n<details><summary>Answer</summary>\n\nx\n</details>") \
        == [], "an answer with no question above it is not a question"
    assert parse_questions("**1. Unclosed?**\n\n<details><summary>Answer</summary>\n\nno end") == []
    assert parse_questions("**1. Empty?**\n\n<details><summary>Answer</summary></details>") == []

    print("selftest ok")


DEFAULT_LIBRARY = Path(__file__).parent / "library"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    sub = p.add_subparsers(dest="cmd")

    t = sub.add_parser("transcribe", help="audio -> notes (default command)")
    t.add_argument("audio", nargs="+", type=Path)
    t.add_argument("--subject", help="subject code or alias, e.g. CY1107 or chem")
    t.add_argument(
        "--lang", default=None, help="force a Whisper language (hi, en). Default: per-chunk detect"
    )
    t.add_argument("--model", default="large-v3", choices=list(WHISPER_REPOS))
    t.add_argument("--notes-lang", default="english", choices=list(NOTES_LANG))
    t.add_argument(
        "--notes-model",
        default="claude-haiku-4-5",
        help="cheapest that works; --notes-model claude-sonnet-5 for harder lectures",
    )
    t.add_argument("--no-notes", action="store_true", help="transcript only, no API call")
    t.add_argument("--outdir", type=Path, help="write here instead of the library")
    t.add_argument("--force", action="store_true", help="redo everything, including transcription")
    t.add_argument("--context", action="store_true",
                   help="attach the subject's slides/PDFs (~2900 tokens per PDF page, so "
                        "a 30-slide deck adds roughly $0.09 to this lecture)")
    t.add_argument(
        "--redo-notes",
        action="store_true",
        help="regenerate notes from the cached transcript (no re-transcribing)",
    )
    t.add_argument("--max-cost", type=float, default=1.00, help="abort above this $ per lecture")
    t.set_defaults(func=None)

    a = sub.add_parser("add", help="file your own notes/slides into a subject")
    a.add_argument("files", nargs="+", type=Path)
    a.add_argument("--subject", help="subject code or alias; inferred from filename if omitted")
    a.add_argument("--outdir", type=Path)
    a.set_defaults(func=add_files)

    s = sub.add_parser("subjects", help="list subjects and what is filed under them")
    s.set_defaults(func=list_subjects)

    f = sub.add_parser("search", help="search across all your notes")
    f.add_argument("query")
    f.set_defaults(func=search)

    rv = sub.add_parser("revise", help="one revision sheet from every lecture in a subject")
    rv.add_argument("subject", help="subject code or alias, e.g. CY1107 or chem")
    rv.add_argument("--notes-model", default="claude-haiku-4-5")
    rv.add_argument("--max-cost", type=float, default=1.00)
    rv.set_defaults(func=revise)

    sv = sub.add_parser("serve", help="open the library on your phone, with Explain")
    sv.add_argument("--host", default=None,
                    help="default 0.0.0.0 (your wifi), but 127.0.0.1 under --no-auth")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--out", type=Path, default=Path(__file__).parent / "site" / "index.html")
    sv.add_argument("--notes-model", default="claude-haiku-4-5")
    sv.add_argument("--max-cost", type=float, default=1.00)
    # Both names, one dest. The flag was --max-explains when Explain was the
    # only thing that called anything, and a deploy script somewhere still
    # says that.
    sv.add_argument("--max-ai", "--max-explains", dest="max_explains",
                    type=int, default=300,
                    help="guard on Groq's daily free tier: stop answering "
                         "after this many taps, across Explain and Ask AI")
    sv.add_argument("--ai-model", default=AI_MODEL,
                    help="the Groq model behind Explain and Ask AI")
    sv.add_argument("--remote-workers", dest="remote", action="store_true",
                    help="hand lectures to `notes.py worker` on another machine "
                         "instead of transcribing them here (the cloud VM has no GPU)")
    sv.add_argument("--no-auth", action="store_true",
                    help="no join screen, no database, no gate, the old single-user "
                         "behaviour, for working on this laptop")
    sv.add_argument("--verbose", action="store_true")
    sv.set_defaults(func=serve)

    w = sub.add_parser("worker", help="transcribe for a server running elsewhere")
    w.add_argument("--server", required=True, help="e.g. https://notes.workwithani.tech")
    w.add_argument("--token", help="default: RECARVE_WORKER_TOKEN, or .env")
    w.add_argument("--workdir", type=Path, default=Path(__file__).parent / ".worker",
                   help="where audio and checkpoints live while a lecture is in hand")
    w.add_argument("--lang", default=None)
    w.add_argument("--model", default="large-v3", choices=list(WHISPER_REPOS))
    w.add_argument("--notes-lang", default="english", choices=list(NOTES_LANG))
    w.add_argument("--notes-model", default="claude-haiku-4-5")
    w.add_argument("--max-cost", type=float, default=1.00)
    w.add_argument("--poll", type=float, default=5.0, help="seconds between polls")
    w.add_argument("--max-poll", type=float, default=120.0,
                   help="the ceiling the backoff climbs to on an empty queue")
    w.add_argument("--timeout", type=float, default=120.0)
    w.set_defaults(func=worker)

    e = sub.add_parser("export", help="build a browsable HTML page of the whole library")
    e.add_argument("--out", type=Path, default=Path(__file__).parent / "site" / "index.html")
    e.set_defaults(func=export)

    tt = sub.add_parser("timetable", help="import a section's week from a CSV")
    tt.add_argument("csv", type=Path, help="day,period,subject_code rows; '-' reads stdin")
    tt.add_argument("--section", help="which section, e.g. --section I")
    tt.add_argument("--grad-year", type=int,
                    help="settles two sections that share a name, e.g. --grad-year 2030")
    tt.add_argument("--dry-run", action="store_true",
                    help="say what would change and write nothing")
    tt.set_defaults(func=import_timetable)

    sub.add_parser("selftest").set_defaults(func=lambda _: selftest())

    # Bare `notes.py lecture.m4a` means transcribe.
    argv = sys.argv[1:]
    if argv and argv[0] not in sub.choices and not argv[0].startswith("-"):
        argv.insert(0, "transcribe")
    args = p.parse_args(argv)

    if not args.cmd:
        p.error("give me an audio file, or try `subjects`")
    if args.cmd != "transcribe":
        return args.func(args)

    if not args.no_notes and not (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ):
        p.error("set ANTHROPIC_API_KEY, or pass --no-notes for transcript only")

    files = collect_audio(args.audio)
    if not files:
        p.error("no audio files found")
    if len(files) > 1:
        print(f"{len(files)} files to process", file=sys.stderr)
    for path in files:
        process(path, args)


if __name__ == "__main__":
    main()
