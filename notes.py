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
  plain text like "lim(h->0) [f(x+h)-f(x)]/h" — it is rendered with KaTeX and plain text stays ugly.
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
    return stem + (f" — {SUBJECTS[code][0].replace('-', ' ')}" if code else "")


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
# Raw string: this is JavaScript, and its backslash escapes are not Python's.
PAGE = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#fcfcfd" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0f1115" media="(prefers-color-scheme: dark)">
<title>recarve — Section I</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/15.0.7/marked.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.11/katex.min.css">
<script defer src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.11/katex.min.js"></script>
<script defer src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.11/contrib/auto-render.min.js"></script>
<style>
:root{
  --bg:#fcfcfd; --surface:#f2f3f7;
  --fg:#14161b; --mut:#656b76; --line:#e1e4ea;
  --accent:#3355e8; --accent-fg:#fff;
  /* Admin ink. Red already means error, so power is not red: it is the page's
     own ink, filled. Accent is every member's action, grey is neutral, ink is
     the handful of things only an admin may press. 14.5:1 either way round. */
  --admin:#232733; --admin-fg:#fcfcfd;
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
  --tap:44px;
  /* 4 (.rows) + 12 (.row) + 3 (.tick) + 12 (gap): where a row's words begin.
     Anything standing in for a row lines up with them, and everything that is
     not a row lines up with the section heading at 16. */
  --hang:31px;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0f1115; --surface:#171a20;
    --fg:#e7e9ee; --mut:#98a0ad; --line:#262a32;
    --accent:#7c93ff; --accent-fg:#0f1115;
    /* Ink inverts with the paper: near-black on near-black is not a slab. */
    --admin:#dfe4f0; --admin-fg:#0f1115;
    --sat:48%; --lum:70%; --chip-lum:22%; --chip-text:78%;
    --warn:#f0bd6a; --warn-bg:#2b2114;
    --err:#ef5f63;
  }
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;background:var(--bg);color:var(--fg);
  font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  overflow-wrap:break-word;
}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
[hidden]{display:none!important}   /* beats the display: on .brand and .shead */

#read{display:none}
body.reading #list{display:none}
body.reading #read{display:block}

.top{
  position:sticky;top:0;z-index:5;background:var(--bg);
  border-bottom:1px solid var(--line);
  padding:max(10px,env(safe-area-inset-top)) 16px 10px;
}
.brand{display:flex;align-items:baseline;gap:8px;min-height:var(--tap);margin-bottom:6px}
.brand b{font-size:20px;letter-spacing:-.015em;font-weight:700}
.brand span{font-size:13px;color:var(--mut)}
#q{
  width:100%;height:var(--tap);padding:0 14px;font-size:16px;
  border:1px solid var(--line);border-radius:11px;background:var(--surface);color:var(--fg);
}
#q::placeholder{color:var(--mut)}

.group{padding:18px 16px 2px}
.code{
  display:inline-block;padding:3px 9px;border-radius:7px;
  font-size:13px;font-weight:650;
  background:hsl(var(--h) var(--sat) var(--chip-lum));
  color:hsl(var(--h) var(--sat) var(--chip-text));
}
.group h2{display:inline;margin:0 0 0 9px;font-size:13px;font-weight:500;color:var(--mut)}
.shead{display:flex;align-items:center;gap:9px;min-height:var(--tap);margin-bottom:6px}
/* The screen's name, at the weight a screen's name is. It ellipsises rather
   than wraps: at this depth the code chip beside it already says which
   subject, so the tail of a long name is the cheapest thing on the row. */
.shead h2{margin:0;font-size:20px;font-weight:700;letter-spacing:-.015em;
  color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sect{margin:0;padding:20px 16px 2px;font-size:13px;font-weight:600;
  letter-spacing:.05em;text-transform:uppercase;color:var(--mut)}
.rows{padding:6px 4px 0}
.row{
  display:flex;align-items:center;gap:12px;width:100%;
  min-height:var(--tap);padding:11px 12px;border-radius:11px;
  text-align:left;text-decoration:none;color:inherit;font-size:16px;
}
.row:active{background:var(--surface)}
.row .tick{width:3px;align-self:stretch;border-radius:2px;background:hsl(var(--h) var(--sat) var(--lum));flex:none}
.row.plain .tick{background:var(--line)}
.row .name{flex:1;min-width:0}
.row .name b{display:block;font-weight:600}
.row .name small{display:block;font-size:13px;color:var(--mut)}
.row .meta{font-size:13px;color:var(--mut);flex:none}
.row .code{flex:none}
.row a.name{text-decoration:none;color:inherit}
@media (hover:hover){
  .row:hover{background:var(--surface)}
  .rank:hover{background:color-mix(in srgb,var(--surface) 60%,transparent)}
  .tabs button:hover{background:var(--surface)}
  .days button:hover:not([aria-current]){background:color-mix(in srgb,var(--fg) 8%,var(--surface))}
  .vote:hover,.mark button:hover,.dpick .step:hover:not([disabled]){border-color:var(--mut)}
  .dock button:hover,.qdock button:hover{filter:brightness(.96)}
}
/* A row that carries its own buttons cannot itself be one, so the name becomes
   the tappable part. Stretched, so the whole height of the row still opens the
   subject rather than a two-line strip in the middle of it. */
.row button.name{padding:0;text-align:left;align-self:stretch}
.blank{margin:16px;padding:22px 20px;border-radius:14px;background:var(--surface);
  border:1px solid var(--line);color:var(--mut);font-size:14px;line-height:1.6}
.blank p{margin:0 0 10px}
.blank p:last-child{margin:0}
/* The first line is the answer -- "Nothing on the notice board yet" -- and the
   rest is why and what to do about it. Said in weight, so the answer is read
   first whether or not the paragraph under it ever is. */
.blank p:first-child{margin-bottom:7px;font-size:16px;line-height:1.45;
  font-weight:650;color:var(--fg)}
/* Reaching for something and not getting it is not the same state as not
   having it yet, and it must not be dressed as one. */
.blank.bad{border-color:var(--err);color:var(--fg)}
.blank.bad p:first-child{color:var(--err)}

/* One vote per person, so this is a two-state toggle and not a counter you can
   lean on. The count sits inside the control: what you are pressing and what
   it did are the same object. */
.vote{display:flex;align-items:center;gap:6px;flex:none;min-height:var(--tap);
  padding:0 12px;border-radius:11px;border:1px solid var(--line);background:var(--surface);
  font-size:13px;font-weight:650;color:var(--mut);font-variant-numeric:tabular-nums}
.vote.on{border-color:var(--accent);color:var(--accent);
  background:color-mix(in srgb,var(--accent) 13%,transparent)}
.vote:active{opacity:.7}
.vote[disabled]{opacity:.45}

/* ---- Attendance. Two buttons, and pressing neither is the third state.
   Never a checkbox: an unticked box reads as "absent", and this app must not
   say that on a student's behalf. The pressed one thickens its keyline as well
   as filling, so the state survives a colourblind reader and a grey screen. */
.mark{display:flex;gap:8px;flex:none}
.mark button{width:var(--tap);height:var(--tap);border-radius:11px;
  border:1px solid var(--line);background:var(--bg);color:var(--mut);
  font-size:16px;line-height:1;display:flex;align-items:center;justify-content:center}
.mark button[aria-pressed="true"]{border-width:2px;font-weight:700}
.mark button.yes[aria-pressed="true"]{border-color:var(--accent);color:var(--accent);
  background:color-mix(in srgb,var(--accent) 13%,transparent)}
.mark button.no[aria-pressed="true"]{border-color:var(--fg);color:var(--fg);
  background:var(--surface)}
.mark button:active{opacity:.7}
.mark button[disabled]{opacity:.45}
/* A class that did not happen. Dashed, because it is a hole in the week and
   not a thing anybody did. */
.off{display:flex;align-items:center;flex:none;min-height:var(--tap);padding:0 12px;
  border:1px dashed var(--line);border-radius:11px;font-size:13px;color:var(--mut)}
/* attended / held / the percentage. The one figure on this screen somebody
   opened this screen for, so it leads the row rather than sharing its weight
   with "Edit your timetable". Tabular, so a column of them does not wobble. */
.row.att .name b{font-size:20px;font-weight:650;letter-spacing:-.012em;
  font-variant-numeric:tabular-nums;line-height:1.3}
/* Below 75%. The number in amber and the word beside it, in the calmest
   arrangement that still cannot be missed -- the student already knows it is
   bad, and what they need off this row is the figure and the next step, not a
   siren. It is on the NUMBER and never on the tick: the tick is the subject's
   own colour and EE1108's is amber, so a healthy EE1108 and a failing CY1107
   read identically the moment the state borrows that bar. */
.row.low .name b{color:var(--warn)}
.flag{flex:none;padding:3px 9px;border-radius:7px;font-size:13px;font-weight:650;
  background:var(--warn-bg);color:var(--warn)}
/* The day picker, shared by the day view and the catch-up screen. Native date
   input: the fastest picker on a phone, nothing to download, and it already
   knows what a month looks like. */
.dpick{display:flex;align-items:center;gap:8px;padding:10px 16px 0}
.dpick input{flex:1;min-width:0;height:var(--tap);padding:0 12px;font:inherit;
  font-size:16px;border:1px solid var(--line);border-radius:11px;
  background:var(--surface);color:var(--fg)}
/* Yesterday and tomorrow, at thumb size. The picker is for jumping a month;
   stepping one day is the move somebody makes walking out of a lecture, and it
   must not cost a modal wheel. */
.dpick .step{flex:none;width:var(--tap);height:var(--tap);border-radius:11px;
  border:1px solid var(--line);background:var(--surface);color:var(--fg);
  font-size:20px;line-height:1}
.dpick .step:active{opacity:.7}
.dpick .step[disabled]{opacity:.35}
.mine{margin:10px 16px 0;padding:16px;border-radius:12px;background:var(--surface)}
.mine .score{font-size:26px;font-weight:700;letter-spacing:-.02em}
.mine p{margin:5px 0 0;font-size:13px;color:var(--mut)}
.tally{display:flex;gap:22px;margin-top:14px}
.tally div{font-size:13px;color:var(--mut)}
.tally b{display:block;font-size:20px;font-weight:650;color:var(--fg);
  font-variant-numeric:tabular-nums}
.mine .sub{margin:6px 0 0;font-size:16px;color:var(--mut)}
.mine .edit{margin-top:14px;min-height:var(--tap);padding:0 14px;border-radius:11px;
  background:var(--bg);border:1px solid var(--line);font-size:16px;color:var(--accent)}
/* On the card, which is itself --surface: a surface chip on a surface card is
   not a chip. The paper behind it plus a keyline is what makes it one. */
.badge{display:inline-block;margin-top:12px;padding:4px 10px;border-radius:7px;
  font-size:13px;font-weight:650;background:var(--bg);border:1px solid var(--line);
  color:var(--mut)}
.badge.trusted{background:color-mix(in srgb,var(--accent) 16%,transparent);
  border-color:var(--accent);color:var(--accent)}
.badge.admin{background:var(--admin);border-color:var(--admin);color:var(--admin-fg)}

/* ---- The class board. A number, a name, a score -- ruled lines rather than
   another stack of identical rounded cards, because twenty cards is a wall and
   not a ranking. Your own row is the one filled one, so finding yourself in it
   costs no reading. */
.rank{display:flex;align-items:center;gap:12px;min-height:var(--tap);
  padding:9px 16px;border-bottom:1px solid var(--line)}
.rank .pos{flex:none;width:2.6em;font-size:13px;color:var(--mut);
  font-variant-numeric:tabular-nums}
.rank .name{flex:1;min-width:0}
.rank .name b,.rank .name small{display:block;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rank .name b{font-weight:600}
.rank .name small{font-size:13px;color:var(--mut)}
/* Earned, not decorative: only shown from Regular up, so a word here means
   somebody did the work. A keyline and the page's own ink, no medal. */
.rank .lvl{flex:none;padding:3px 8px;border-radius:7px;font-size:13px;
  font-weight:650;border:1px solid var(--line);color:var(--mut)}
.rank .pts{flex:none;min-width:2.4em;text-align:right;font-size:20px;
  font-weight:650;font-variant-numeric:tabular-nums}
.rank.you{background:var(--surface);box-shadow:inset 3px 0 0 var(--accent)}
.rank.you .pos,.rank.you .pts{color:var(--accent)}

/* ---- The notice board. Ruled entries, not another stack of rounded cards:
   a notice is a piece of writing with a title over it, and a box around every
   one of them turns six notices into six identical boxes. Pinned is a rule
   down the left edge in the accent -- the same weight as a row's tick, so the
   two screens read as one app. */
.ann{padding:16px;border-bottom:1px solid var(--line)}
.ann.pin{border-left:3px solid var(--accent);padding-left:13px}
/* Set back by its furniture, not by its ink: an alpha on the card dimmed
   the one word that explains the state. The rule down the edge goes grey,
   the flag takes the page's muted ink, and every string still reads. */
.ann.gone{border-left:3px solid var(--line);padding-left:13px}
.ann h3{margin:0;font-size:20px;line-height:1.3;font-weight:650;letter-spacing:-.012em}
.ann .meta{margin:5px 0 0;font-size:13px;color:var(--mut)}
.ann .flag{font-weight:650;color:var(--accent)}
.ann.gone .flag{color:var(--mut)}
.ann .md{margin-top:11px;font-size:16px}
.ann .md>:first-child{margin-top:0}
.ann .md>:last-child{margin-bottom:0}
.ann .md p{margin:11px 0}
.ann .md ul,.ann .md ol{padding-left:22px;margin:11px 0}
.ann .md li{margin:4px 0}
.ann .md h1,.ann .md h2,.ann .md h3{font-size:16px;font-weight:700;margin:15px 0 4px}
.ann .md a{color:var(--accent)}
.ann .md code{background:var(--surface);padding:2px 5px;border-radius:5px;font-size:.92em}
.ann .md pre{background:var(--surface);padding:12px;border-radius:11px;overflow-x:auto;font-size:13px}
.ann .md img{max-width:100%;height:auto}
/* A pasted table is the one thing in a body that can be wider than a phone.
   It scrolls inside itself rather than taking the page sideways with it. */
.ann .md table{display:block;overflow-x:auto;min-width:0}
/* Editing and hiding are admin ink, the same ink as every other control only
   an admin may press. */
.ann .acts{display:flex;gap:8px;margin-top:13px}
/* Outlined rather than a filled slab: two inked slabs under every notice an
   admin owns outweigh the notice itself. Ink on the page's own paper, keyed
   by a keyline -- distinct from every member's control, and 14.5:1 either way
   round. */
.ann .acts button{min-height:var(--tap);padding:0 15px;border-radius:11px;
  background:var(--bg);border:1px solid var(--admin);color:var(--admin);
  font-size:13px;font-weight:650}
.ann .acts button:active{opacity:.75}

/* ---- Doubts: the thread under a note. Ruled entries, not cards -- a question
   and the answers to it are one piece of writing between people, and a box
   around each one turns a conversation into a list of unrelated things. The
   answers are set in from the question by a rule, which is the only nesting
   this page has and the only nesting the table allows. */
#doubts{max-width:70ch;margin:0 auto;padding:0 18px 142px}
#doubts h2{margin:38px 0 0;font-size:20px;line-height:1.3;letter-spacing:-.012em;
  font-weight:650;padding-bottom:7px;border-bottom:1px solid var(--line)}
#doubts .quiet{padding:14px 0 0}
.dbt{padding:16px 0;border-bottom:1px solid var(--line)}
/* Somebody's typing, drawn as typing: pre-wrap keeps their line breaks and
   textContent is what puts it there. Nothing in this block is ever parsed. */
.said{margin:0;font-size:16px;white-space:pre-wrap;overflow-wrap:anywhere}
.dbt>.said{font-weight:600;line-height:1.5}
.dbt .meta{margin:5px 0 0;font-size:13px;color:var(--mut)}
.ans{display:flex;gap:10px;align-items:flex-start;
  margin:14px 0 0 2px;padding:0 0 0 13px;border-left:2px solid var(--line)}
.ans .what{flex:1;min-width:0}
/* The one the class voted up. Said in the rule, never by dimming the others,
   which are still answers worth reading. */
.ans.top{border-left-color:var(--accent)}
#doubts .acts{display:flex;flex-wrap:wrap;gap:8px;margin-top:11px}
#doubts .acts button{min-height:var(--tap);padding:0 15px;border-radius:11px;
  background:var(--bg);border:1px solid var(--line);color:var(--accent);
  font-size:13px;font-weight:650}
/* Taking down somebody else's is an admin act, marked as one: the page's own
   ink on a keyline, the same treatment as every other admin control. */
#doubts .acts button.adm{border-color:var(--admin);color:var(--admin)}
#doubts .acts button:active{opacity:.75}
.askbox{padding:14px 0 2px}
.askbox textarea{width:100%;min-height:5.5em;font-size:16px;line-height:1.6;
  padding:11px 12px;border:1px solid var(--line);border-radius:11px;
  background:var(--surface);color:var(--fg);font-family:inherit;resize:vertical}
.askbox .err{margin:8px 0 0;font-size:13px;color:var(--err);min-height:1.2em}
.askbox button{display:block;width:100%;min-height:var(--tap);padding:0 18px;
  border-radius:11px;background:var(--accent);color:var(--accent-fg);
  font-size:16px;font-weight:600}
.askbox button:active{opacity:.75}

/* The composer: a title, a body, and whether it sits at the top. */
.compose{padding:8px 16px 16px}
.compose label{display:block;font-size:13px;color:var(--mut);margin:14px 0 5px}
.compose input,.compose textarea{width:100%;font-size:16px;
  border:1px solid var(--line);border-radius:11px;background:var(--surface);
  color:var(--fg);font-family:inherit}
.compose input{min-height:var(--tap);padding:0 12px}
.compose textarea{min-height:9.5em;line-height:1.6;padding:11px 12px;resize:vertical}
.compose .pinrow{display:flex;align-items:center;gap:11px;min-height:var(--tap);
  margin-top:14px;font-size:16px;color:var(--fg)}
.compose .pinrow input{width:22px;height:22px;min-height:0;flex:none;accent-color:var(--accent)}
.compose .err{margin:10px 0 0;font-size:13px;color:var(--err);min-height:1.2em}
.compose .go{display:flex;gap:8px;margin-top:6px}
.compose .go button{flex:1;min-height:var(--tap);border-radius:11px;background:var(--bg);
  border:1px solid var(--line);font-size:16px;font-weight:500}
.compose .go button.primary{background:var(--admin);color:var(--admin-fg);border-color:transparent}

/* Your own two fields, in the card they replace. */
.pform label{display:block;font-size:13px;color:var(--mut);margin:14px 0 5px}
.pform input{width:100%;min-height:var(--tap);font-size:16px;padding:0 12px;
  border:1px solid var(--line);border-radius:11px;background:var(--bg);color:var(--fg)}
.pform .err{margin:10px 0 0;font-size:13px;color:var(--err);min-height:1.2em}
.pform .go{display:flex;gap:8px;margin-top:14px}
.pform .go button{flex:1;min-height:var(--tap);border-radius:11px;background:var(--bg);
  border:1px solid var(--line);font-size:16px;font-weight:500}
.pform .go button.primary{background:var(--accent);color:var(--accent-fg);border-color:transparent}

/* ---- Admin ink, the same treatment as the admin screen so the two read as
   one thing: an inked tick down the row, and the word on the end of it. */
.row.adm .tick{background:var(--admin)}
.tag{flex:none;padding:3px 8px;border-radius:6px;background:var(--admin);
  color:var(--admin-fg);font-size:13px;font-weight:650}

/* ---- Locked. A student sees the control, is told who it is for and what
   opens it, and never spends a request to find out. Muted on surface is
   5.2:1 light and 7.2:1 dark -- this is greyed, not unreadable. */
#fab.locked{background:var(--surface);color:var(--mut);border:1px solid var(--line);
  box-shadow:none}
#ask.locked{background:var(--surface);color:var(--mut);border:1px solid var(--line);
  box-shadow:0 6px 20px rgba(0,0,0,.18)}
#sheet .opt.locked{background:transparent;border-style:dashed;color:var(--mut)}
#sheet .opt.locked b{color:var(--mut)}
#lock{margin-top:14px;padding:14px;border-radius:12px;background:var(--surface);
  border:1px solid var(--line);font-size:13px;color:var(--fg)}
#lock b{display:block;font-size:16px;font-weight:650;margin-bottom:5px}

.rtop{display:flex;align-items:center;gap:6px}
.back{display:flex;align-items:center;gap:4px;height:var(--tap);padding:0 8px 0 4px;
  margin-left:-4px;font-size:16px;color:var(--accent);font-weight:500;flex:none}
.rtop .code{margin-left:auto}
/* Four sizes only -- 26/20/16/13, roughly a 1.25 step. h3 separates itself by
   weight and colour rather than a fifth size that would read as body text. */
/* The dock's clearance moved to #doubts, which is the last thing on the
   reading screen now: 142px is #nav's, and the FAB reaches 76 + 58 = 134. */
article{padding:22px 18px 8px;max-width:70ch;margin:0 auto}
article h1{font-size:26px;line-height:1.2;letter-spacing:-.022em;font-weight:700;margin:0 0 24px}
article h2{font-size:20px;line-height:1.3;letter-spacing:-.012em;font-weight:650;
  margin:38px 0 12px;padding-bottom:7px;border-bottom:1px solid var(--line)}
article h3{font-size:16px;font-weight:700;color:var(--accent);margin:26px 0 6px}
article ul,article ol{padding-left:22px}
article li{margin:5px 0}
article code{background:var(--surface);padding:2px 5px;border-radius:5px;font-size:.92em}
article pre{background:var(--surface);padding:13px;border-radius:11px;overflow-x:auto;font-size:13px}
article img{max-width:100%;height:auto}
.katex-display{overflow-x:auto;overflow-y:hidden;padding:4px 0}
.scroll-x{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:12px 0}
table{border-collapse:collapse;font-size:16px;min-width:100%}
td,th{border:1px solid var(--line);padding:8px 12px;text-align:left}
th{background:var(--surface)}
details{margin:9px 0;background:var(--surface);border-radius:11px;overflow:hidden}
summary{min-height:var(--tap);display:flex;align-items:center;padding:0 14px;color:var(--accent);font-size:16px;font-weight:500}
details[open] summary{border-bottom:1px solid var(--line)}
details>:not(summary){padding:0 14px}

.dock{
  position:fixed;left:0;right:0;bottom:0;z-index:6;display:flex;gap:8px;
  padding:9px 14px calc(9px + env(safe-area-inset-bottom));
  background:color-mix(in srgb,var(--bg) 88%,transparent);
  backdrop-filter:blur(12px);border-top:1px solid var(--line);
}
.dock button{
  flex:1;min-height:var(--tap);border-radius:11px;background:var(--surface);
  font-size:16px;font-weight:500;display:flex;align-items:center;justify-content:center;
}
.dock button.primary{background:var(--accent);color:var(--accent-fg);font-weight:650}
/* Share, Download and Print are things you might do to a note; Practice is the
   thing you opened it to do. They stay full-size targets and step back a
   little in weight so the primary is the one the eye lands on. */
.dock button:not(.primary){color:var(--mut);font-size:15px}
/* Below .top's 5: it is placed in document coordinates, so a scroll can
   carry it into the sticky header, where it used to paint over the back
   button and eat the tap meant for it. */
#ask{position:absolute;z-index:4;display:none;padding:9px 15px;border-radius:10px;
  background:var(--accent);color:var(--accent-fg);font-size:16px;font-weight:600;
  box-shadow:0 6px 20px rgba(0,0,0,.28)}
#ask.on{display:block}
#panel{position:fixed;left:0;right:0;bottom:0;z-index:10;transform:translateY(101%);
  transition:transform .22s ease;background:var(--bg);border-top:1px solid var(--line);
  border-radius:16px 16px 0 0;max-height:76dvh;display:flex;flex-direction:column;
  box-shadow:0 -8px 34px rgba(0,0,0,.22)}
#panel.on{transform:none}
@media (prefers-reduced-motion:reduce){#panel{transition:none}}
#panel header{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--line)}
#panel header b{font-size:16px;flex:1}
#panel .quote{font-size:13px;color:var(--mut);padding:10px 16px 0;
  overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
#panel .out{padding:12px 16px calc(20px + env(safe-area-inset-bottom));overflow-y:auto;font-size:16px}
#panel .out .katex-display{overflow-x:auto}
#close{min-width:var(--tap);min-height:var(--tap);font-size:16px;color:var(--mut)}
#fab{position:fixed;right:16px;bottom:calc(76px + env(safe-area-inset-bottom));z-index:7;
  width:58px;height:58px;border-radius:50%;background:var(--accent);color:var(--accent-fg);
  font-size:30px;line-height:1;box-shadow:0 6px 22px rgba(0,0,0,.3)}
/* #ask is clamped to innerWidth-130, which on a 390px screen puts its right
   edge inside the FAB's band -- and the FAB is z-index 7 above #ask's 4, so a
   tap there opened the Add sheet instead. They are never both wanted. */
body:has(#ask.on) #fab{display:none}
#sheet{position:fixed;inset:0;z-index:11;display:none;background:rgba(0,0,0,.45)}
#sheet.on{display:block}
#sheet .card{position:absolute;left:0;right:0;bottom:0;background:var(--bg);
  border-radius:16px 16px 0 0;padding:18px 16px calc(18px + env(safe-area-inset-bottom));
  max-height:88dvh;overflow-y:auto}
#sheet h3{margin:0 0 14px;font-size:20px}
#sheet label{display:block;font-size:13px;color:var(--mut);margin:14px 0 6px}
#sheet select,#sheet .opt{width:100%;min-height:var(--tap);font-size:16px;border-radius:11px;
  border:1px solid var(--line);background:var(--surface);color:var(--fg);padding:0 12px}
#sheet .opt{display:flex;flex-wrap:wrap;align-items:center;gap:0 11px;
  padding:9px 12px;margin-top:9px;text-align:left;line-height:1.4}
#sheet .opt b{font-weight:600}
#sheet .opt span{flex:1 0 100%;color:var(--mut);font-size:13px}
#rec{margin-top:14px;padding:16px;border-radius:12px;background:var(--surface);text-align:center;display:none}
#rec.on{display:block}
#rec .time{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
#rec .dot{display:inline-block;width:11px;height:11px;border-radius:50%;background:var(--err);
  margin-right:8px;animation:pulse 1.4s infinite}
@keyframes pulse{50%{opacity:.25}}
@media (prefers-reduced-motion:reduce){#rec .dot{animation:none}}
#prog{margin-top:16px;display:none}
#prog.on{display:block}
#prog .bar{height:10px;border-radius:5px;background:var(--surface);overflow:hidden}
#prog .fill{height:100%;width:0;background:var(--accent);transition:width .18s linear}
#prog .txt{margin-top:9px;font-size:13px;color:var(--mut);text-align:center}
#prog.err .fill{background:var(--err)}
.job .bar{height:4px;border-radius:2px;background:var(--line);margin-top:6px;overflow:hidden}
.job .bar i{display:block;height:100%;background:var(--accent);width:0;transition:width .3s}
.job .col{flex:1;min-width:0}
#logbtn{width:100%;min-height:var(--tap);border-radius:11px;
  font-size:13px;font-weight:500;color:var(--mut);text-align:left}
#logbtn:active{background:var(--surface)}
#logbox{display:none;margin-top:8px;padding:12px;border-radius:11px;background:var(--surface);
  font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre;
  overflow-x:auto;max-height:44vh;overflow-y:auto}
#logbox.on{display:block}
#jobs{padding:0 16px}
.job{display:flex;align-items:center;gap:11px;padding:11px 12px;margin-top:8px;
  border-radius:11px;background:var(--surface);font-size:16px}
.job .st{font-size:13px;color:var(--mut);margin-left:auto;text-align:right}
.job.failed{box-shadow:inset 3px 0 0 var(--err)}
.job.failed .st{color:var(--err);font-weight:650}
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
.waitmsg{margin:9px 0 0;font-size:13px;color:var(--mut)}
/* Anchored at the bottom, clear of the FAB (76 + 58) and the dock, and never
   takes a tap. It used to float over the header, where it covered the back
   button and the search box and ate the taps meant for them. */
#busy{position:fixed;z-index:6;display:none;pointer-events:none;left:12px;right:12px;
  bottom:calc(144px + env(safe-area-inset-bottom));max-width:34rem;margin:0 auto;
  padding:13px 15px;border-radius:12px;background:var(--surface);
  border:1px solid var(--line);box-shadow:0 8px 26px rgba(0,0,0,.22)}
#busy.on{display:block}
.dock button:active{opacity:.75}
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
  flex:1;min-height:var(--tap);border-radius:11px;font-size:13px;font-weight:500;
  color:var(--mut);display:flex;align-items:center;justify-content:center;
}
.tabs button[aria-current]{color:var(--accent);font-weight:650}
.tabs button:active{background:var(--surface)}
body.reading .tabs{display:none}
/* Clear of the bar AND of the FAB above it (76 + 58), so the last row is never
   half under either. 80px cleared only the bar, and the FAB then sat on top of
   the last row's vote button with no scroll left to escape it. */
#nav{padding-bottom:calc(142px + env(safe-area-inset-bottom))}

/* ---- Home: a plain line of prose where a row would lie. -------------- */
.quiet{padding:10px 16px 10px var(--hang);margin:0;font-size:13px;color:var(--mut)}

/* ---- The timetable editor. One day at a time, eight native selects: the
   iOS wheel is the fastest subject picker on a phone and it costs nothing to
   download. Six days of tapping is under two minutes, which is the whole
   design brief for this screen. */
.days{display:flex;gap:6px;padding:14px 16px 6px;overflow-x:auto;-webkit-overflow-scrolling:touch}
.days button{flex:none;min-height:var(--tap);padding:0 15px;border-radius:11px;
  background:var(--surface);font-size:16px;color:var(--mut)}
.days button[aria-current]{background:var(--accent);color:var(--accent-fg);font-weight:650}
.slot{display:flex;align-items:center;gap:12px;padding:5px 16px}
.slot span{flex:none;width:5.2em;font-size:13px;color:var(--mut)}
.slot select{flex:1;min-width:0;min-height:var(--tap);font-size:16px;padding:0 10px;
  border:1px solid var(--line);border-radius:11px;background:var(--surface);color:var(--fg)}
.save{display:block;width:calc(100% - 32px);margin:18px 16px 0;min-height:var(--tap);
  border-radius:11px;background:var(--accent);color:var(--accent-fg);
  font-size:16px;font-weight:600}
.save:active{opacity:.75}

/* ---- Practice: one question at a time, over everything else. ---------- */
#quiz{position:fixed;inset:0;z-index:12;background:var(--bg);display:flex;flex-direction:column}
.qtop{display:flex;align-items:center;gap:12px;flex:none;
  padding:max(10px,env(safe-area-inset-top)) 16px 10px;border-bottom:1px solid var(--line)}
.qtop b{flex:1;font-size:16px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#qcount{font-size:13px;color:var(--mut);font-variant-numeric:tabular-nums;flex:none}
#qexit{min-width:var(--tap);min-height:var(--tap);font-size:16px;color:var(--mut);flex:none}
#qbar{flex:none;height:3px;background:var(--line)}
#qbar i{display:block;height:100%;width:0;background:var(--accent);transition:width .2s}
#qmain{flex:1;overflow-y:auto;width:100%;max-width:70ch;margin:0 auto;padding:24px 18px 28px}
#qsrc{margin:0 0 12px;font-size:13px;color:var(--mut)}
#qq{font-size:20px;line-height:1.4;font-weight:600}
#qq p{margin:0 0 10px}
#qq .katex-display{font-weight:400}
#qscore{font-size:26px;font-weight:700;margin:0 0 6px}
#qsub{margin:0;color:var(--mut)}
#qa{margin-top:24px;padding-top:20px;border-top:1px solid var(--line);font-size:16px}
#qa>:first-child{margin-top:0}
.qdock{flex:none;display:flex;gap:8px;padding:9px 14px calc(9px + env(safe-area-inset-bottom));
  border-top:1px solid var(--line)}
.qdock button{flex:1;min-height:var(--tap);border-radius:11px;background:var(--surface);
  font-size:16px;font-weight:500;display:flex;align-items:center;justify-content:center}
.qdock button.primary{background:var(--accent);color:var(--accent-fg)}
.qdock button:active{opacity:.75}
/* Practice makes the reading dock four buttons wide on a phone. */
.dock button{white-space:nowrap}

@media (min-width:760px){
  body{display:flex}
  #list{width:320px;flex:none;border-right:1px solid var(--line);height:100dvh;overflow-y:auto;position:sticky;top:0}
  #read{flex:1;display:block;min-width:0}
  body.reading #list{display:block}
  .rtop .back{display:none}   /* the list is already on screen next to it */
  article{padding:30px 40px 110px}
  .dock{left:320px}
  body:not(.reading) .dock{display:flex}
  /* Two panes, two bars: the tabs stay under the list they navigate, and the
     dock starts where the note does, so reading no longer costs the tabs. */
  .tabs{right:auto;width:320px}
  body.reading .tabs{display:flex}
}
@media print{
  .top,.dock,.tabs,#list,.rtop,#fab,#busy,#ask,#quiz{display:none!important}
  #read{display:block!important}
  article{padding:0;max-width:none}
  details{background:none;border:1px solid #999}
}
</style>

<section id="list">
  <div class="top">
    <div class="brand" id="brand"><b>recarve</b><span>Section I</span></div>
    <div class="shead" id="shead" hidden>
      <button class="back" id="lback" aria-label="Back to all subjects">&lsaquo; Subjects</button>
      <span class="code" id="scode"></span>
      <h2 id="sname"></h2>
    </div>
    <input id="q" placeholder="Search notes and transcripts" autocomplete="off" enterkeyhint="search">
  </div>
  <div id="jobs"></div>
  <div id="tools" style="padding:0 16px" hidden>
    <button id="logbtn">Show activity log</button>
    <pre id="logbox"></pre>
  </div>
  <nav id="nav"></nav>
</section>

<section id="read">
  <div class="top rtop">
    <button class="back" id="back" aria-label="Back to the subject">&lsaquo; Back</button>
    <span class="code" id="rcode"></span>
  </div>
  <article id="body"><p class="blank">Pick a lecture to start reading.</p></article>
  <section id="doubts" aria-label="Doubts"></section>
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
      An admin makes you one &mdash; ask in your class group and say what you want
      to add. Until then everything here is still yours to read, search,
      practise from and upvote.
    </div>
    <button class="opt" id="opt-rec"><b>Record this class</b><span>keep the screen on</span></button>
    <button class="opt" id="opt-audio"><b>Upload a recording</b><span>m4a, mp3, mp4 &middot; up to __AUDIO_MB__ MB</span></button>
    <button class="opt" id="opt-doc"><b>Upload notes or slides</b><span>pdf, txt, md &middot; up to __DOC_MB__ MB</span></button>
    <button class="opt" id="opt-revise"><b>Make a revision sheet</b><span>from every lecture in this subject</span></button>
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
  <button data-tab="home">Home</button>
  <button data-tab="classes">Classes</button>
  <button data-tab="campus">Campus</button>
  <button data-tab="me">Me</button>
</nav>

<script>
const DATA = __DATA__;
const nav = document.getElementById('nav'), body = document.getElementById('body');
const backBtn = document.getElementById('back');
const q = document.getElementById('q'), rcode = document.getElementById('rcode');
const brand = document.getElementById('brand'), shead = document.getElementById('shead');
const scode = document.getElementById('scode'), sname = document.getElementById('sname');
const lback = document.getElementById('lback'), tools = document.getElementById('tools');
const tabBtns = document.querySelectorAll('.tabs button');
let current = null;                      // the note being read, or null
let currentCode = null;                  // its subject's code, alongside it
// Which tab, and how deep inside it. Classes goes subject -> note; Home has
// one level under it, the timetable editor.
let view = {tab: 'home', code: null, title: null, edit: false, att: false};

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
const TABS = ['home', 'classes', 'campus', 'me'];
const TAB_TITLE = {classes: 'Subjects', campus: 'Campus', me: 'Your profile'};
const subjectOf = code => DATA.find(s => s.code === code);
const lecturesOf = s => s.notes.filter(n => n.kind !== 'revision');
const revisionOf = s => s.notes.find(n => n.kind === 'revision');
const plural = (n, word) => n + ' ' + word + (n === 1 ? '' : 's');

function counts(s) {
  const bits = [];
  if (lecturesOf(s).length) bits.push(plural(lecturesOf(s).length, 'lecture'));
  if (s.uploads.length) bits.push(plural(s.uploads.length, 'note'));
  if (revisionOf(s)) bits.push('revision sheet');
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

function noteRow(n, s) {
  const b = document.createElement('button');
  b.className = 'row';
  b.style.setProperty('--h', hue(s.code));
  b.innerHTML = '<i class="tick"></i><span class="name"><b></b><small></small></span>';
  b.querySelector('b').textContent = n.title;
  // Blank until the server says who: the static export has no database behind
  // it, and "recorded by nobody" would be a worse answer than silence.
  b.querySelector('small').textContent = n.by ? 'recorded by ' + n.by : '';
  b.onclick = () => go('classes', s.code, n.title);
  return b;
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
        body: JSON.stringify(answer ? {answer: u.id, on: !u.voted}
                                    : {id: u.id, on: !u.voted}),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || 'could not register that vote');
      // Refetch rather than patch: the vote changes the ranking too, and one
      // source of order beats two that can disagree.
      await (answer ? loadDoubts() : refresh());
    } catch (e) {
      b.disabled = false;
      busyDone(e.message);
    }
  };
  return b;
}

function fileRow(u, s) {
  const el = document.createElement('div');
  el.className = 'row';
  el.style.setProperty('--h', hue(s.code));
  el.innerHTML = '<i class="tick"></i>'
               + '<a class="name" target="_blank" rel="noopener"><b></b><small></small></a>';
  const a = el.querySelector('a');
  a.href = u.path;
  a.querySelector('b').textContent = u.name;
  a.querySelector('small').textContent = u.by ? 'added by ' + u.by : 'file';
  // No id means no row behind it: a static export, or a file the database has
  // not adopted. Showing a vote button that cannot work is worse than none.
  if (u.id) el.appendChild(voteBtn(u));
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
  return quiet(shut ? shut.title + ' — no classes.'
                    : 'No classes on ' + DAYS[day] + '.');
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

// 0. WHAT'S COMING. The institute's own calendar, reduced to the one thing a
// student actually wants from it: how far away is the next thing that matters.
// Nothing to show until ATT has answered -- there is no guessed countdown, the
// same way there is no guessed timetable.
function countdownBlock() {
  if (!ATT || !ATT.next) return;
  const n = ATT.next;
  const today = attToday();
  const days = Math.round(
    (new Date(n.date + 'T00:00:00') - new Date(today + 'T00:00:00')) / 86400000);
  const ends = Math.round(
    (new Date(n.ends + 'T00:00:00') - new Date(today + 'T00:00:00')) / 86400000);
  const short = d => MONTHS[+d.slice(5, 7) - 1].slice(0, 3) + ' ' + (+d.slice(8));
  let sub;
  if (days <= 0 && ends >= 0) {
    sub = ends > 0 ? 'On now · ends ' + short(n.ends) : 'On now · ends today';
  } else if (days === 1) {
    sub = 'Tomorrow';
  } else {
    sub = 'In ' + days + ' days · ' + short(n.date);
  }
  block('Coming up', [line(n.title, sub, document.createElement('div'))]);
}

// 1. TODAY. Empty is the honest first state: nobody has typed a timetable in,
// and the institute PDF's columns are ambiguous enough that a guessed one
// would quietly file lectures under the wrong subject.
function todayBlock() {
  // The server's day when there is a server, because the marks written from
  // this block are stamped with the server's date. A handset a few hours out
  // would otherwise list Sunday's classes and file them under Monday.
  const day = ATT ? dayOfISO(ATT.today) : dayOf(new Date());
  if (TT === null) return block('Today', [quiet('Checking your timetable…')]);

  const edit = line('Edit your timetable', 'Add or change a period',
                    document.createElement('button'));
  edit.onclick = () => go('home', 'timetable');

  if (!TT.length) {
    if (!live) {
      return block('Today', [quiet('Your timetable needs the server. Run: notes.py serve')]);
    }
    const start = line('Set up your timetable',
                       'Six days, one tap per class · about a minute',
                       document.createElement('button'));
    start.onclick = () => go('home', 'timetable');
    return block('Today', [start]);
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
  if (ATT) {
    const back = line('Catch up on another day', 'Mark a class you forgot',
                      document.createElement('button'));
    back.onclick = () => go('home', 'attendance');
    rows.push(back);
  }
  rows.push(edit);
  block('Today \u00b7 ' + DAYS[day], rows);
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
  if (ROLE !== 'admin') return;
  const a = document.createElement('a');
  a.href = '/admin';
  block('Admin', [inked(line('Class admin',
    'Members, roles, the invite link, and anything reported', a))]);
}

function renderHome() {
  noticeBlock();
  countdownBlock();
  todayBlock();
  needsBlock();
  adminBlock();
  savedBlock();
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
function savedBlock() {
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
  block('Saved', rows);
}

// The one level inside Home. Six days of native selects: the iOS wheel is the
// fastest subject picker on a phone, costs nothing to download, and is the
// difference between filling this in and giving up on it.
let draft = null, draftDay = 1;
const slotKey = (d, p) => d + '-' + p;

function renderTimetable() {
  if (!draft) {
    draft = {};
    (TT || []).forEach(s => { draft[slotKey(s.day, s.period)] = s.code; });
    draftDay = dayOf(new Date()) || 1;      // Sunday has no column: start at Monday
  }

  const days = document.createElement('div');
  days.className = 'days';
  for (let d = 1; d <= 6; d++) {
    const b = document.createElement('button');
    b.textContent = DAYS[d].slice(0, 3);
    b.setAttribute('aria-label', DAYS[d]);
    if (d === draftDay) b.setAttribute('aria-current', 'true');
    b.onclick = () => { draftDay = d; render(); };
    days.appendChild(b);
  }
  nav.appendChild(days);

  for (let p = 1; p <= PERIODS; p++) {
    const row = document.createElement('div');
    row.className = 'slot';
    row.innerHTML = '<span></span>';
    row.querySelector('span').textContent = 'Period ' + p;
    const sel = document.createElement('select');
    sel.setAttribute('aria-label', DAYS[draftDay] + ', period ' + p);
    const free = document.createElement('option');
    free.value = ''; free.textContent = '— free —';
    sel.appendChild(free);
    for (const s of DATA) {
      const o = document.createElement('option');
      o.value = s.code; o.textContent = s.code + ' — ' + s.name;
      sel.appendChild(o);
    }
    sel.value = draft[slotKey(draftDay, p)] || '';
    sel.onchange = () => { draft[slotKey(draftDay, p)] = sel.value; };
    row.appendChild(sel);
    nav.appendChild(row);
  }

  const save = document.createElement('button');
  save.className = 'save';
  save.textContent = 'Save timetable';
  save.onclick = saveTimetable;
  nav.appendChild(save);
  nav.appendChild(quiet('Periods are numbered, not timed. The institute grid does '
                        + 'not say which hour is which clearly enough to print one.'));
}

async function saveTimetable() {
  const slots = Object.keys(draft).filter(k => draft[k]).map(k => ({
    day: +k.split('-')[0], period: +k.split('-')[1], code: draft[k]}));
  busy('Saving your timetable…', true);
  try {
    const r = await fetch('/timetable', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({slots}),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || 'could not save that');
    TT = slots;
    draft = null;
    busyDone(slots.length ? 'Timetable saved' : 'Timetable cleared');
    history.back();
  } catch (e) {
    busyDone('Could not save that: ' + e.message);
  }
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
                   label + ' — period ' + slot.period + ', ' + slot.code);
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
  if (!ATT) return block('Attendance', [quiet('Checking your attendance…')]);
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
  // Their own, because that is what the database allows: "admins edit their
  // own announcements" refuses anybody else's, and offering a button that
  // would be refused is worse than not offering it.
  if (a.mine && ROLE === 'admin') {
    const acts = document.createElement('div');
    acts.className = 'acts';
    const edit = document.createElement('button');
    edit.textContent = 'Edit';
    edit.onclick = () => go('campus', a.id);
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
    busyDone(payload.deleted ? 'Hidden — you can still put it back'
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

function renderAnnouncements() {
  heading('Announcements');
  if (ROLE === 'admin') {
    const post = line('Post an announcement', 'Every approved classmate sees it',
                      document.createElement('button'));
    post.onclick = () => go('campus', 'new');
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
      ROLE === 'admin'
        ? 'Anything the whole section needs to know goes here — a moved '
          + 'lab, a deadline, where the lecture is. Everyone approved sees it, '
          + 'and pinned ones stay at the top.'
        : 'This is where the section is told things — a moved lab, a '
          + 'deadline, where a lecture is. Your class admin puts them up, and '
          + 'new ones show on Home until you have read them.');
  }
  list.forEach(a => nav.appendChild(annCard(a)));
  markRead(list);
}

// On HOME: pinned, or not yet read. A notice you have read and nobody pinned
// is not what you need right now, and three at most either way -- a wall of
// old notices at the top of Home is how people learn to scroll past the top
// of Home.
function noticeBlock() {
  const show = liveAnn().filter(a => a.pinned || a.unread);
  if (!show.length) return;
  const rows = show.slice(0, 3).map(a => {
    const b = line(a.title, [a.unread ? 'New' : '', a.pinned ? 'Pinned' : '',
                             ago(a.at, NOW)].filter(Boolean).join(' · '),
                   document.createElement('button'));
    b.onclick = () => go('campus');
    return b;
  });
  if (show.length > rows.length) {
    rows.push(quiet('and ' + (show.length - rows.length) + ' more, under Campus.'));
  }
  block('Notices', rows);
}

// ---- CAMPUS: the class, not your own shelf. -----------------------------
// The board lives here rather than on Me because it is a list of other people.
// Me is where your own score and the rules behind it are, next to your name and
// your role; a ranking of a hundred and ten classmates is not a fact about you.
// Campus is also the tab that had nothing in it, and a leaderboard is the first
// thing the class as a whole actually owns.
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
  el.querySelector('.pos').textContent = r.score ? '#' + r.rank : '—';
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
      + 'Points are recognition only — every note in the library is open to '
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

async function renderCampus() {
  const mine = painted;
  // Writing a notice takes the tab over: it is one thing at a time on a phone,
  // and the board underneath is not what you are doing.
  if (view.compose && ROLE === 'admin') return renderCompose();
  renderAnnouncements();
  heading('Who has contributed');
  windowPicker();
  const box = document.createElement('div');
  box.className = 'mine';
  nav.appendChild(box);
  nav.appendChild(quiet('Clubs and events will live here too, next to the '
    + 'notice board above. Nothing is made up: this fills with what your own '
    + 'clubs put here.'));
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
  if (TT === null) return block(dayName(date), [quiet('Checking your timetable…')]);
  if (!TT.length) {
    if (!live) {
      return block(dayName(date),
        [quiet('Your timetable needs the server. Run: notes.py serve')]);
    }
    const start = line('Set up your timetable',
                       'Six days, one tap per class · about a minute',
                       document.createElement('button'));
    start.onclick = () => go('home', 'timetable');
    return block(dayName(date), [start]);
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
  block(dayName(date), rows);
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
  block('Your day', [today]);

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
    mark.onclick = () => go('home', 'attendance');
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
    b.querySelector('small').textContent =
      plural(all.length, 'question') + ' from every note in ' + s.code;
    b.onclick = () => qOpen(s, null);
    block('Practice', [b]);
  }
  block('Lectures', lecturesOf(s).map(n => noteRow(n, s)));
  block('Notes & slides', s.uploads.map(u => fileRow(u, s)));
  const rev = revisionOf(s);
  block('Revision sheet', rev ? [noteRow(rev, s)] : []);
  if (!s.notes.length && !s.uploads.length) {
    saying('Nothing in ' + s.code + ' yet.',
           'Tap + to record a class or to add slides you already have. It files '
           + 'itself here, and the notes come back when the Mac has made them.');
  }
}

// THE ME TAB: who you are, what your role lets you do, and what you have put
// in. Plus -- if you run the class library -- the way into the admin panel.
//
// Points are status and nothing else. Nothing in this app asks for a score
// before it shows you something, and no lock on this page is opened by one:
// the locks are roles, and the only thing that moves a role is an admin.
const ROLE_TITLE = {student: 'Student', trusted: 'Trusted member', admin: 'Admin'};
const ROLE_SAYS = {
  student: 'Read everything the class has, search it, practise from it, and '
         + 'upvote the notes that helped.',
  trusted: 'Everything a student can, and add notes and slides, record a class, '
         + 'build a revision sheet, and use Explain.',
  admin: 'Everything a trusted member can, and let people in, set what each of '
       + 'them may do, and hold the invite code.',
};
// Said in exactly one place, and read by the sheet, the Explain panel and this
// screen. A lock that gives three different reasons is three locks.
const LOCK_ADD = 'Adding to the library is for trusted members. An admin makes '
  + 'you one — ask in your class group and say what you want to add.';
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
  + 'trusted — ask in your class group.';

// Your own two fields, in the card they replace. Nothing else on this screen
// is yours to change: /profile writes name and phone, and the database pins
// status and role to what they already are whatever the request asks for.
function profileCard(box, d) {
  box.className = 'mine';
  box.innerHTML = '<div class="score"></div><p class="sub"></p>'
                + '<div><span class="badge"></span></div>';
  box.querySelector('.score').textContent = d.name || 'You';
  box.querySelector('.sub').textContent =
    [d.roll_no, d.section, d.phone].filter(Boolean).join(' · ');
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
      profileCard(box, d);
      busyDone('Saved');
    } catch (e) {
      err.textContent = e.message;
    }
  };
}

async function renderMe() {
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
  profileCard(box, d);

  // What the role actually permits, said once, on the screen somebody comes to
  // after a control elsewhere told them it was not theirs. A student is not
  // told off for being one: they are told what they have and what opens more.
  const can = [line(ROLE_TITLE[d.role] || 'Student',
                    ROLE_SAYS[d.role] || ROLE_SAYS.student)];
  if (d.role === 'student') can.push(line('What trusted adds', LOCK_ADD));
  block('Your access', can);

  const p = d.points;
  heading('Contributions');
  const tally = document.createElement('div');
  tally.className = 'mine';
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
  nav.appendChild(tally);

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
      const b = line(d.invite, 'Invite code — tap to copy',
                     document.createElement('button'));
      const cap = b.querySelector('small');
      b.onclick = async () => {
        try { await navigator.clipboard.writeText(d.invite); flash(cap, 'Copied'); }
        catch { flash(cap, 'Copy failed — read it out'); }
      };
      rows.push(inked(b));
    } else {
      rows.push(inked(line('No invite code is live',
                           'Nobody can join until there is one')));
    }
    block('Admin', rows);
  }

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
                     + 'not only their titles \u2014 so a word you remember the '
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
  // Search is the Classes tab's own tool; it must not answer over Campus.
  const needle = view.tab === 'classes' ? q.value.trim().toLowerCase() : '';
  const s = view.code ? subjectOf(view.code) : null;
  nav.innerHTML = '';
  // Home keeps the brand; every other tab names itself in the same header,
  // and #lback -- the only back button at this depth -- appears only where
  // there is a level above to climb to.
  brand.hidden = view.tab !== 'home' || view.edit || view.att;
  shead.hidden = view.tab === 'home' && !view.edit && !view.att;
  lback.hidden = !s && !view.edit && !view.att && !view.compose && !view.day;
  scode.hidden = !s;
  q.hidden = view.tab !== 'classes';
  tools.hidden = view.tab !== 'me';
  // Home shows the same jobs under "Needs you"; two copies of a running
  // transcription on one screen is one copy too many.
  jobsBox.hidden = view.tab === 'home';
  // The one back button at this depth serves two levels now, so it has to say
  // which one it climbs to.
  lback.textContent = (view.edit || view.att) ? '‹ Home'
    : (view.compose ? '‹ Campus' : '‹ Subjects');
  lback.setAttribute('aria-label', (view.edit || view.att) ? 'Back to Home'
    : (view.compose ? 'Back to the notice board' : 'Back to all subjects'));
  if (s) {
    scode.textContent = s.code;
    scode.style.setProperty('--h', hue(s.code));
  }
  sname.textContent = s ? s.name
    : view.day ? 'Your day'
    : view.edit ? 'Your timetable'
    : view.att ? 'Your attendance'
    : view.compose ? (view.compose === 'new' ? 'New notice' : 'Edit notice')
    : TAB_TITLE[view.tab];
  tabBtns.forEach(b => {
    if (b.dataset.tab === view.tab) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  if (needle) renderSearch(needle);
  else if (view.edit) renderTimetable();
  else if (view.att) renderAttendance();
  else if (view.day) renderDay();
  else if (view.tab === 'home') renderHome();
  else if (view.tab === 'campus') renderCampus();
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
  const tab = TABS.includes(parts[0]) ? parts[0] : 'home';
  const s = tab === 'classes' ? subjectOf(parts[1]) : null;
  const title = s ? parts[2] : null;
  // The timetable editor is a level inside Home and therefore a URL, so back
  // climbs out of it exactly like every other step.
  const edit = tab === 'home' && parts[1] === 'timetable';
  // And so is catching up on attendance, for the same reason.
  const att = tab === 'home' && parts[1] === 'attendance';
  // The day view is the same shape one tab over: a level inside Classes, so
  // back climbs to the subject list rather than out of the app. 'day' can
  // never collide with a subject -- every code is letters and digits.
  const dayv = tab === 'classes' && parts[1] === 'day';
  // So is the composer, for the same reason: without a URL the back gesture
  // dropped what was typed and left the Campus tab stuck on an empty form.
  const compose = tab === 'campus' ? parts[1] || null : null;
  view = {tab, code: s ? s.code : null, title: title || null, edit, att, compose,
          day: dayv};
  if (!edit) draft = null;      // walking away drops an unsaved week, not TT
  if (!att) attDate = null;     // and re-opening it starts on today, not last week
  if (!dayv) dayDate = null;    // today by default, every time it is opened
  const n = s && title ? s.notes.find(x => x.title === title) : null;
  if (n) openNote(n, s); else closeRead();
  render();
}

// Markdown in, typeset HTML out. The note, the Explain panel and a practice
// question all wanted this and each had grown its own copy of the delimiters.
function mdInto(el, md) {
  el.innerHTML = marked.parse(md || '');
  if (window.renderMathInElement) {
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
let THREAD = [];        // the open note's questions, newest first
let threadOn = null;    // {subject, title} the thread belongs to, or null
const doubtsBox = document.getElementById('doubts');

// Typing, drawn as typing. The single most important line in this section.
function said(x) {
  const p = document.createElement('p');
  p.className = 'said';
  p.textContent = x.body;
  return p;
}

function saidBy(x) {
  const m = document.createElement('p');
  m.className = 'meta';
  m.textContent = x.by + ' · ' + ago(x.at, NOW);
  return m;
}

// Taking your own words back, or -- inked as the power it is -- somebody
// else's. Offered to nobody else, because the policy would only refuse them.
function dropBtn(x) {
  if (!x.mine && ROLE !== 'admin') return null;
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
  el.append(said(q), saidBy(q));
  const reply = document.createElement('button');
  reply.textContent = 'Answer this';
  const row = acts(reply, dropBtn(q));
  el.appendChild(row);
  // The box appears where it was asked for and only there: a form under every
  // question on the screen is six forms nobody asked for.
  reply.onclick = () => {
    reply.disabled = true;
    el.insertBefore(askBox('Answer ' + q.by + '…', 'Post this answer', q.id), row);
  };
  // Only the top answer is marked, and only when the class actually voted for
  // it: a rule down the side of the one answer with no votes says nothing.
  q.answers.forEach((a, k) => el.appendChild(answerCard(a, k === 0 && a.votes > 0)));
  return el;
}

// One line instead of the thread when there is a reason -- still loading, or
// no server behind this copy of the page at all. The heading stays either way,
// so the section does not appear and disappear under the note as it loads.
function drawDoubts(instead) {
  doubtsBox.innerHTML = '';
  const h = document.createElement('h2');
  h.textContent = 'Doubts';
  doubtsBox.appendChild(h);
  if (instead) return doubtsBox.appendChild(quiet(instead));
  doubtsBox.appendChild(askBox(
    'Ask the section about this. Somebody who was there will know.',
    'Ask the section', null));
  if (!THREAD.length) {
    return doubtsBox.appendChild(quiet(
      'No questions on this one yet. Asking is worth as much as answering — '
      + 'if you are stuck, somebody else is too.'));
  }
  THREAD.forEach(q => doubtsBox.appendChild(doubtCard(q)));
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
async function loadDoubts(note, subject) {
  if (note) threadOn = {subject: subject.code, title: note.title};
  if (!threadOn) return;
  const asked = threadOn;
  if (note) { THREAD = []; drawDoubts('Looking for questions…'); }
  try {
    const r = await fetch('/doubts?subject=' + encodeURIComponent(asked.subject)
                        + '&title=' + encodeURIComponent(asked.title));
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    if (threadOn !== asked) return;   // they tapped through while this flew
    THREAD = d.doubts || [];
    drawDoubts();
  } catch {
    // A static export has no server to ask, and a form that cannot post is
    // worse than a sentence saying why.
    THREAD = [];
    drawDoubts('Questions need the server. Run: notes.py serve');
  }
}

// LEVEL 3: the note itself. Called only by route(), which has already put the
// right URL in the bar, so this never touches history.
function openNote(n, s) {
  current = n;
  currentCode = s.code;
  rcode.textContent = s.code;
  rcode.style.setProperty('--h', hue(s.code));
  backBtn.textContent = '‹ ' + s.code;   // back goes to the subject, not the top
  mdInto(body, n.md);
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
  loadDoubts(n, s);
}

function closeRead() {
  document.body.classList.remove('reading');
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
    .flatMap(n => questionsOf(n).map(x => ({q: x.q, a: x.a, from: n.title})));
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

function qOpen(s, note) {
  const items = quizItems(s, note);
  if (!items.length) return;
  const key = 'recarve.quiz.' + s.code + (note ? '/' + note.title : '');
  const was = qLoad(key, items.length);
  qz = {key, items, oneNote: !!note, order: was ? was.order : items.map((_, k) => k),
        i: was ? was.i : 0, marks: was ? was.marks : []};
  qtitle.textContent = note ? note.title : 'Practice ' + s.name;
  quiz.hidden = false;
  qStep();
}

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
  qsrc.textContent = '';
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
function qMark(ok) { qz.marks[qz.i] = ok ? 1 : 0; qz.i++; qSave(); qStep(); }
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
    PENDING = d.pending || 0;
    ANN = d.announcements || [];
    NOW = d.now || NOW;
    ROLE = d.role || ROLE;
    applyRole();
    markSeen(d.now);
    if (!subj.options.length) {
      for (const c of d.codes) {
        const o = document.createElement('option');
        o.value = c.code; o.textContent = c.code + ' — ' + c.name;
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
      if (view.tab === 'home' && !view.edit) render();
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
const mayAdd = () => ROLE === 'trusted' || ROLE === 'admin';
function applyRole() {
  const fab = document.getElementById('fab');
  // Unknown is not a role. Until /data answers there is nothing honest to say
  // about the + button, so it waits rather than appearing and then locking --
  // and a 503 or a 403 leaves ROLE null, which is not permission either.
  fab.hidden = ROLE === null;
  fab.className = mayAdd() ? '' : 'locked';
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
};
document.getElementById('fab').onclick = openSheet;
document.getElementById('opt-close').onclick = closeSheet;
sheet.onclick = e => { if (e.target === sheet) closeSheet(); };

const prog = document.getElementById('prog'), fill = document.getElementById('fill');
const ptxt = document.getElementById('ptxt');
const mb = b => (b / 1048576).toFixed(1) + ' MB';

// XMLHttpRequest, not fetch: fetch cannot report upload progress at all, so a
// big lecture over wifi looks frozen and people give up mid-transfer.
const LIMIT_AUDIO = __AUDIO_MB__ * 1048576, LIMIT_DOC = __DOC_MB__ * 1048576;
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
    ptxt.textContent = `${name} is ${mb(blob.size)} — the limit is ${mb(cap)}`;
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
    ptxt.textContent = msg + (retry ? ' — tap an option to try again' : '');
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

// Each option refuses to start rather than opening a file picker, filling a
// progress bar and coming back 403. The reason is already on screen above
// them, in #lock, so there is nothing left for the tap to say.
document.getElementById('opt-audio').onclick = () => {
  if (!mayAdd()) return;
  fileInput.accept = 'audio/*,video/*'; fileInput.click();
};
document.getElementById('opt-doc').onclick = () => {
  if (!mayAdd()) return;
  fileInput.accept = '.pdf,.txt,.md'; fileInput.click();
};
fileInput.onchange = () => {
  const f = fileInput.files[0];
  if (f) upload(f, f.name);
  fileInput.value = '';
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
""".replace("__AUDIO_MB__", str(MAX_AUDIO_BYTES // 1048576)).replace("__DOC_MB__", str(MAX_DOC_BYTES // 1048576))


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
        raise SystemExit(f"no library at {lib} yet — transcribe or add something first")

    out = args.out
    data = build_data(lib, out.parent)

    if not any(s["notes"] or s["uploads"] for s in data):
        # Not fatal any more: the page lists the twelve subjects, and it is
        # where you go to put the first thing into one of them.
        print("library is empty — the page will list the subjects and nothing else",
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
    out.write_text(f"# Revision — {SUBJECTS[code][0].replace('-', ' ')}\n\n{text}\n")
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
ENV_PATH = Path(__file__).resolve().parent / ".env"

# Requests are gated before they are dispatched, so a route added later is
# protected whether or not whoever adds it remembers auth exists. These are the
# only paths that opt out, and adding to this set is the deliberate act.
PUBLIC_PATHS = {"/join", "/login"}

# Three roles, in order. A student reads everything the class has; trusted adds
# the things that write content or spend money on the API; admin adds the class
# itself. status is the other axis and is checked separately -- a pending admin
# is still pending.
SECTION = "Section I"

ROLES = ("student", "trusted", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}

# What each endpoint costs, in the same place as PUBLIC_PATHS and for the same
# reason: the gate below reads this before dispatch, so a route added later is
# refused to everyone but an admin until somebody names its price here. The
# unnamed ones -- /data, /jobs, /log, /vote, /timetable, /doubts, the library
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
    "/reset": "admin",
    "/announce": "admin",     # posting to a hundred and ten people at once
    # Calling a class off changes everybody's denominator, so it is the same
    # bar as adding to the library. Marking your OWN attendance is not here:
    # it is a private note to yourself and every approved member makes them.
    "/cancelled": "trusted",
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
        # The first person in is the admin. Nobody can approve anybody
        # otherwise, so the invite that bootstraps the class also elects them.
        act_as(conn, None, local=True)
        if conn.execute("select count(*) from profiles").fetchone()[0] == 1:
            # role 'admin', which carries trusted with it: an untrusted
            # uploader's files are forced pending by the materials trigger, and
            # the admin is the one person nobody else can ever publish.
            conn.execute(
                "update profiles set status = 'approved', role = 'admin' "
                "where id = %s",
                (user_id,),
            )
        row = conn.execute("select status, role from profiles where id = %s",
                           (user_id,)).fetchone()
        result = (user_id, row[0], row[1] == "admin")
    return result


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
    """Move somebody between the three roles.

    Admin-gated in the database by the "admins manage profiles" policy, so a
    member's connection changes nobody -- this function only decides the two
    things the policy cannot see: that the role is one of the three, and that
    an admin is not demoting themselves. The second is not paranoia about
    privilege, it is about the class: the admin is the only account that can
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
    """
    n = conn.execute("update materials set status = 'removed' where id = %s",
                     (material_id,)).rowcount
    if not n:
        raise ValueError("no such item")
    return True


def db_profile(conn, user_id):
    """The identity half of the Me tab. Read as themselves."""
    row = conn.execute(
        "select name, roll_no, phone, role, status from profiles where id = %s",
        (user_id,),
    ).fetchone()
    if not row:
        return {}
    return {"name": row[0], "roll_no": row[1], "phone": row[2],
            "role": row[3], "status": row[4]}


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


def db_invite(conn):
    """The live code an admin passes on, or None if there is not one.

    Read as whoever is asking: invites has no select policy for members, so a
    normal session sees an empty table here rather than a code it could hand
    to the whole college. Never mints one -- issuing invites is a decision,
    not something a screen does on its own while being looked at.
    """
    row = conn.execute(
        "select code from invites where expires_at > now() and uses < max_uses "
        "order by expires_at desc limit 1"
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
    return conn.execute("select approve_uploader(%s)", (profile_id,)).fetchone()[0]


def db_record_upload(conn, user_id, code, filename, dest, is_audio):
    """Remember who sent a file, so the library can say so later."""
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
            "size_bytes) values (%s, %s, %s, %s, %s)",
            (code, user_id, filename, str(dest), dest.stat().st_size),
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
    for mid, code, filename, who, votes, mine in conn.execute(
        "select m.id, m.subject_code, m.filename, p.name, "
        "  (select count(*) from votes v where v.material_id = m.id), "
        "  exists (select 1 from votes v "
        "           where v.material_id = m.id and v.voter_id = %s) "
        "from materials m join profiles p on p.id = m.uploader_id",
        (user_id,),
    ):
        mats[(code, filename)] = {"id": str(mid), "by": who,
                                  "votes": votes, "voted": mine}
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

    `col` says which kind of thing is being voted for -- an upload, or an
    answer to somebody's doubt. It is one of exactly two identifiers, both
    written down right here and neither of them ever taken off a request: the
    handler maps its own shape of payload onto one of these two words, so
    there is nothing a caller can put in the middle of that f-string.
    """
    if col not in ("material_id", "doubt_id"):
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


def db_doubts(conn, user_id, code, lecture_id=None):
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
        # Live only. The select policy leaves an author their own hidden row --
        # it has to, or hiding one would be refused for having hidden it -- so
        # what the class reads is asked for here. q.deleted_at is null is true
        # for a question, which has no parent to have been hidden.
        "  and d.deleted_at is null and q.deleted_at is null "
        "order by d.created_at",
        {"me": user_id, "code": code, "lec": lecture_id},
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


def db_ask(conn, user_id, code, lecture_id, parent_id, body):
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
        code, lecture_id = None, None
    return str(conn.execute(
        "insert into doubts (subject_code, lecture_id, parent_id, author_id, body) "
        "values (%s, %s, %s, %s, %s) returning id",
        (code, lecture_id, parent_id, user_id, body),
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

    One function because it is one row and one policy: "admins edit their own"
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


DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
PERIODS = 8


def db_timetable(conn, user_id):
    """One student's week, in the order Home reads it.

    Nobody has typed the institute grid into this app, and the source PDF's
    columns are ambiguous enough that guessing one would mis-file lectures in
    silence. So it starts empty and the student fills it in.
    """
    return [
        {"day": day, "period": period, "code": code}
        for day, period, code in conn.execute(
            "select day, period, subject_code from timetable "
            "where profile_id = %s order by day, period",
            (user_id,),
        )
    ]


def db_set_timetable(conn, user_id, slots):
    """Replace the whole week. Returns the rows written.

    Edited whole and never bigger than 48 rows, so replace beats a diff: there
    is no half-saved state to reason about, and clearing a period is the same
    operation as setting one.

    Validates here rather than in the handler -- the check constraints and the
    foreign key would refuse bad input anyway, but as a 500, and every caller
    routes through this one function.
    """
    clean = {}
    for s in slots:
        try:
            day, period = int(s["day"]), int(s["period"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("a slot needs a day and a period")
        code = s.get("code")
        if not 1 <= day <= 6:
            raise ValueError(f"day {day} is not Monday to Saturday")
        if not 1 <= period <= PERIODS:
            raise ValueError(f"period {period} is not 1 to {PERIODS}")
        if code not in SUBJECTS:
            raise ValueError(f"unknown subject {code!r}")
        clean[(day, period)] = code            # last write wins; the PK would raise
    with conn.transaction():
        conn.execute("delete from timetable where profile_id = %s", (user_id,))
        for (day, period), code in sorted(clean.items()):
            conn.execute(
                "insert into timetable (profile_id, day, period, subject_code) "
                "values (%s, %s, %s, %s)",
                (user_id, day, period, code),
            )
    return len(clean)


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
            "on conflict (on_date, period, subject_code) do nothing",
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


GATE_PAGE = r"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>recarve — Section I</title>
<style>
:root{color-scheme:light dark;--bg:#fcfcfd;--fg:#14161b;--mut:#656b76;--line:#e1e4ea;
  --accent:#3355e8;--accent-fg:#fff;--err:#d1344b;
  /* Admin ink. Not a bolted-on red -- red is the error colour and already
     means something. This is the page's own ink, filled: the accent stays the
     ordinary blue action, grey stays neutral, and a solid slab of ink is what
     only an admin can press. The same pair marks the same thing inside the
     app, on the Me tab, so the treatment is one thing in two files. */
  --admin:#232733;--admin-fg:#fcfcfd;--surface:#f2f3f7}
@media (prefers-color-scheme:dark){
  /* The accent goes pale in the dark, so what sits on it has to go dark too --
     white on it is 2.8:1. Same pair PAGE carries. */
  :root{--bg:#0f1115;--fg:#e7e9ee;--mut:#98a0ad;--line:#262a32;
    --accent:#7a92ff;--accent-fg:#0f1115;--err:#e5484d;
    /* Ink inverts with the paper: a near-black slab on a near-black ground is
       not a slab. Same job, same contrast, opposite end of the ramp. */
    --admin:#dfe4f0;--admin-fg:#0f1115;--surface:#171a20}}
*{box-sizing:border-box}
body{margin:0;min-height:100dvh;display:grid;place-items:center;padding:24px;background:var(--bg);
  color:var(--fg);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
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
   WhatsApp link used to arrive at a bare form: five boxes, no answer to what
   the class is being asked to join. The form did not change -- it is now the
   bottom of a page that shows the thing first. Every other body in this file
   is still the plain card, so none of these class names appears on them. */
.mark{font-size:1.25rem;font-weight:700;letter-spacing:-.02em}
.mark span{color:var(--mut);font-weight:400;margin-left:.5rem;font-size:.875rem}
h1.big{font-size:1.6rem;line-height:1.25;margin:1.3rem 0 .4rem;max-width:20ch}
.sub{color:var(--mut);margin:0 0 1.3rem;max-width:34ch}
/* Showing the transformation beats describing it: it is the one thing this
   app does that a classmate cannot picture from a sentence. */
.demo{border:1px solid var(--line);border-radius:14px;overflow:hidden}
.side{padding:.8rem .95rem}
.side+.side{border-top:1px solid var(--line)}
.cap{font-size:.75rem;color:var(--mut);margin:0 0 .45rem}
.said,.became{font-size:.9rem;line-height:1.55}
/* The English words inside the Hindi are the point of the example, not
   emphasis -- em is the markup, grey is the reading. */
.said em{font-style:normal;color:var(--mut)}
.became b{display:block;font-size:.8rem;color:var(--mut);font-weight:500;margin:0 0 .25rem}
.became ul{margin:0;padding-left:1.1rem}
.became li{margin:.2rem 0}
/* A formula is one token with no spaces in it, on a 360px screen. */
.became code{font-size:.85rem;background:var(--surface);padding:1px 5px;border-radius:5px;
  overflow-wrap:anywhere}
.arrow{display:flex;align-items:center;gap:.55rem;padding:.55rem .95rem;
  background:var(--surface);font-size:.78rem;color:var(--mut)}
.arrow i{flex:none;width:6px;height:6px;border-radius:50%;background:var(--accent)}
.pts{margin:1.4rem 0 0;padding:0;list-style:none}
.pts li{display:flex;gap:.7rem;padding:.6rem 0;border-bottom:1px solid var(--line);
  font-size:.95rem}
.pts li:last-child{border-bottom:0}
.pts b{flex:none;width:1.4em;color:var(--accent);font-weight:700}
.join{border-top:1px solid var(--line);margin-top:1.6rem;padding-top:1.5rem}
.join h2{font-size:1.25rem;margin:0 0 .25rem}
</style>
<main>__BODY__</main>
"""

JOIN_BODY = r"""<div class="mark">recarve<span>Section I</span></div>
<h1 class="big">Every lecture, written down.</h1>
<p class="sub">One person records the class. Everyone gets the notes.</p>

<div class="demo">
  <div class="side">
    <p class="cap">What the professor said</p>
    <div class="said">&ldquo;&#2340;&#2379; &#2360;&#2348;&#2360;&#2375; &#2346;&#2361;&#2354;&#2375;
      &#2361;&#2350; <em>limit</em> &#2325;&#2366; <em>concept</em> &#2360;&#2350;&#2333;&#2375;&#2306;&#2327;&#2375;,
      &#2347;&#2367;&#2352; <em>power rule</em> &#2342;&#2375;&#2326;&#2375;&#2306;&#2327;&#2375;&rdquo;</div>
  </div>
  <div class="arrow"><i></i> transcribed and written up, about ten minutes later</div>
  <div class="side">
    <p class="cap">What you get</p>
    <div class="became">
      <b>Key points</b>
      <ul>
        <li>The limit is the basis of the derivative</li>
        <li>Power rule: <code>d/dx x&#8319; = n&#183;x&#8319;&#8315;&#185;</code></li>
      </ul>
    </div>
  </div>
</div>

<ul class="pts">
  <li><b>1</b><span>Hindi and English mixed, the way your professors actually talk.</span></li>
  <li><b>2</b><span>Practice questions from every lecture, answers hidden until you want them.</span></li>
  <li><b>3</b><span>Everyone&rsquo;s notes and slides in one place, per subject &mdash; not scattered
  across chats.</span></li>
</ul>

<div class="join">
<h2>Join your section</h2>
__INVITED__
<p>__INTRO__</p>
<form id="f">
  <label for="nm">Name</label><input id="nm" required autocomplete="name">
  <label for="roll">Roll number</label><input id="roll" required autocomplete="off">
  <label for="ph">Phone number</label>
  <input id="ph" required type="tel" inputmode="tel" autocomplete="tel">
  <label for="sec">Section</label>
  <input id="sec" value="Section I" readonly tabindex="-1">
  <label for="code">Invite code</label>
  <input id="code" required autocomplete="off" autocapitalize="off" value="__CODE__">
  <label for="pw">Password</label>
  <p class="hint">At least __MIN__ characters, and not your roll number. Use
  something you do not use anywhere else.</p>
  <input id="pw" required type="password" autocomplete="new-password" minlength="__MIN__">
  <button>Join</button>
  <p class="err" id="err"></p>
</form>
<p class="hint">An admin approves you before you can read the class&rsquo;s notes.</p>
</div>
<p><a href="/login">Already joined? Log in</a></p>
<script>
const $ = i => document.getElementById(i);
$('f').onsubmit = async e => {
  e.preventDefault();
  $('err').textContent = '';
  // /join opens a database connection before it replies. Left alone the button
  // looks dead on mobile data, and the second tap races the first: one wins,
  // the other comes back 409 and tells the joiner they already exist.
  const b = $('f').querySelector('button');
  b.disabled = true;
  b.textContent = 'Joining\u2026';
  try {
    const res = await fetch('/join', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: $('nm').value, roll_no: $('roll').value,
                            phone: $('ph').value, code: $('code').value,
                            password: $('pw').value})});
    const j = await res.json().catch(() => ({}));
    if (res.ok) return location.reload();
    $('err').textContent = j.error || 'could not join';
  } catch (e) { $('err').textContent = 'no connection to the server'; }
  b.disabled = false;
  b.textContent = 'Join';
};
</script>
""".replace("__MIN__", str(MIN_PASSWORD))


def join_body(code="", inviter=None):
    """The join form, carrying whatever the invite link brought with it.

    Both substitutions are escaped. The code is a query parameter a stranger
    writes, and the name is whatever an admin typed into this same form on the
    day they joined -- neither is a place to run script from.

    The code is not checked here, on purpose: a page anyone can load that says
    whether a code is good is a code checker. It is validated where a typed one
    is, by join_with_invite when the form is submitted, and a bad one fails
    there with the same sentence whichever way it arrived.
    """
    line = f'<p class="by">Invited by {html.escape(inviter)}</p>' if inviter else ""
    # Telling somebody who tapped a link that they need a code they can see in
    # the box is how a form reads as broken before it has been used.
    intro = ("Section I notes. Your code is already in — add your details."
             if code else
             "Section I notes. You need the invite code from someone already in.")
    return (JOIN_BODY.replace("__INVITED__", line)
                     .replace("__INTRO__", intro)
                     .replace("__CODE__", html.escape(code, quote=True)))


LOGIN_BODY = r"""<h1>recarve</h1>
<p>Section I notes. Sign in with your roll number.</p>
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
knows your roll number can sign in as you — and a roll number is on every list
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
approve you — leave it open, it rechecks itself.</p>
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
<p>Students read. Trusted members upload, record and use Explain &mdash; that one
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
  for (const r of ['student', 'trusted', 'admin']) {
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
"""


EXPLAIN_PROMPT = """A student is reading their lecture notes and highlighted a passage they do not
understand. Explain just that passage.

Rules:
- 3-5 sentences. This is a quick doubt, not a lecture.
- Plain language first, then the technical statement.
- If it is a formula, say what each symbol means and when you would use it.
- Maths as LaTeX: $...$ inline, $$...$$ display.
- Answer only what was highlighted. Do not summarise the whole note.
- If the passage is too fragmentary to explain, say so and ask what specifically is unclear."""


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
    budget = {"left": args.max_explains}
    jobs = Jobs(args)
    logins = Limiter()

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
            log(f"nobody has joined yet — whoever uses invite code {first} first "
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
        def log_message(self, fmt, *a):
            if args.verbose:
                super().log_message(fmt, *a)

        def translate_path(self, path):
            if path.split("?")[0] in ("/", "/index.html"):
                return str(args.out)
            return super().translate_path(path)

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
            import urllib.parse

            code = urllib.parse.parse_qs(
                self.path.partition("?")[2]).get("code", [""])[0].strip()[:64]
            inviter = None
            try:
                with db() as conn:
                    inviter = db_inviter(conn, code)
            except psycopg.Error as e:
                # Who is inviting them is a nicety; being able to join is not.
                log(f"cannot say who is inviting: {e}", "join")
            return join_body(code, inviter)

        def principal(self):
            """Whose session this is, re-read from the database every request.

            Not cached in the cookie: blocking someone has to take effect on
            their next tap, not whenever their cookie happens to expire.
            """
            jar = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            morsel = jar.get(SESSION_COOKIE)
            profile_id = unsign_session(morsel.value, secret) if morsel else None
            if not profile_id:
                return None
            with db(profile_id) as conn:
                return db_principal(conn, profile_id)

        def do_GET(self):
            if self.path == "/sw.js":
                return self.send_js(SW_JS)
            if self.path.split("?")[0] == "/login":
                return self.send_html(GATE_PAGE.replace("__BODY__", LOGIN_BODY))
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
            if self.path.split("?")[0] == "/doubts":
                return self.do_doubts_get()
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

        def is_admin(self):
            return bool(self.me and self.me["admin"])

        def do_POST(self):
            if self.path == "/join":
                return self.do_join()
            if self.path == "/login":
                return self.do_login()
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
            if self.path == "/bookmark":
                return self.do_bookmark()
            if self.path == "/timetable":
                return self.do_timetable()
            if self.path == "/attendance":
                return self.do_attendance()
            if self.path == "/cancelled":
                return self.do_cancelled()
            if self.path == "/announce":
                return self.do_announce()
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

                import anthropic

                msg = anthropic.Anthropic().messages.create(
                    model=args.notes_model,
                    max_tokens=1200,
                    system=EXPLAIN_PROMPT,
                    messages=[{"role": "user", "content":
                               f"From the note \"{req.get('title', '')}\":\n\n{text}"}],
                )
                out = "".join(b.text for b in msg.content if b.type == "text")
                hit.write_text(out)
                rate_in, rate_out = price_of(args.notes_model)
                cost = msg.usage.input_tokens / 1e6 * rate_in + msg.usage.output_tokens / 1e6 * rate_out
                log(f"~${cost:.4f}, {budget['left']} left, cached {key[:8]}", "explain", 1)
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
                # apart at all.
                #
                # ponytail: a client can write that header, so this half is a
                # brake on a naive grinder rather than a wall. The per-roll
                # limit is the one that protects an account, and nothing the
                # caller sends can move it -- it is keyed on the roll number
                # they are guessing at.
                fwd = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                client = fwd[:64] or self.client_address[0]
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
            """Move somebody between student, trusted and admin.

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
                       error if over else f"{error} — retrying ({attempts} of {MAX_ATTEMPTS})")
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

            try:
                n = int(self.headers.get("Content-Length", 0))
                if n <= 0:
                    return self.reply(400, {"error": "empty upload"})

                raw = urllib.parse.unquote(self.headers.get("X-Filename", "upload"))
                # Never trust a client-supplied filename with a path in it.
                name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(raw).name)[:120] or "upload"
                subject = self.headers.get("X-Subject", "").strip()
                code = resolve_subject(subject) if subject else guess_subject(name)
                if not code:
                    return self.reply(400, {"error": f"pick a subject for {name}"})

                ext = Path(name).suffix.lower()
                is_audio = ext in AUDIO_EXTS
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
                        "error": f"{name} is {mb(n, up=True)} — the limit for {what} "
                                 f"is {mb(cap)}", "limit": cap, "size": n})
                dest = (inbox / f"{code}-{name}") if is_audio \
                    else (subject_dir(args.library, code, "uploads") / name)

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
                        db_record_upload(conn, self.me["id"], code, name, dest, is_audio)
                job = jobs.add(dest, code, "audio" if is_audio else "document")
                return self.reply(200, {"job": job})
            except SystemExit as e:
                return self.reply(400, {"error": str(e)})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})

        def do_vote(self):
            """One vote per person per item, and never for your own.

            Two shapes of payload, one route: {"id": ...} is an upload and
            {"answer": ...} is somebody's answer to a doubt. They share this
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
            """(subject code, lecture id) for the thread a request names.

            One place, because the GET reads it and the POST reads it and a
            question has to land on the thread the reader is looking at.

            The phone names a note the way the library does -- a subject and a
            title -- and the lecture id is looked up here. A title with no row
            behind it is not an error: the thread is then the subject's, which
            is the one every note in it can fall back to.
            """
            code = (req.get("subject") or "").strip()[:32]
            if code not in SUBJECTS:
                raise ValueError("which subject?")
            return code, db_lecture_id(conn, code,
                                       (req.get("title") or "").strip()[:200])

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
                    code, lecture = self.thread_asked_for(
                        conn, {k: v[0] for k, v in q.items()})
                    thread = db_doubts(conn, self.me["id"], code, lecture)
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
                    code, lecture = self.thread_asked_for(conn, req)
                    if req.get("delete"):
                        db_hide_doubt(conn, (req.get("id") or "").strip())
                    else:
                        db_ask(conn, self.me["id"], code, lecture,
                               (req.get("parent") or "").strip() or None,
                               req.get("body"))
                    thread = db_doubts(conn, self.me["id"], code, lecture)
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

        def do_timetable(self):
            """Save the whole week. It comes back down inside /data.

            No GET of its own: Home already fetches /data on the way in, and a
            second request for six rows is a second thing to be slow.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n > 20000:
                    self.close_connection = True
                    return self.reply(413, {"error": "too much"})
                req = json.loads(self.rfile.read(n) or b"{}")
                slots = req.get("slots")
                if not isinstance(slots, list):
                    return self.reply(400, {"error": "expected a list of slots"})
                with db(self.me["id"]) as conn:
                    saved = db_set_timetable(conn, self.me["id"], slots)
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            return self.reply(200, {"saved": saved})

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
            """Post, edit, hide or restore one notice. Admins only.

            Admin-gated twice over, like every other admin route: the gate
            refused everybody else before this was reached, and the write runs
            on the caller's own connection where "admins edit their own
            announcements" has to allow it too -- which is what makes "their
            own" true rather than intended.
            """
            if not self.is_admin():
                return self.reply(403, {"error": "admins only", "required": "admin"})
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

    return ThreadingHTTPServer((args.host, args.port), partial(Handler, directory=str(root)))


def serve(args):
    """Serve the library and answer 'explain this' taps from the phone.

    The API key never leaves the Mac: the phone posts the highlighted text here
    and gets prose back.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("no ANTHROPIC_API_KEY set - Explain will return an error", file=sys.stderr)

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
    sv.add_argument("--max-explains", type=int, default=300,
                    help="spend guard: stop answering after this many taps")
    sv.add_argument("--remote-workers", dest="remote", action="store_true",
                    help="hand lectures to `notes.py worker` on another machine "
                         "instead of transcribing them here (the cloud VM has no GPU)")
    sv.add_argument("--no-auth", action="store_true",
                    help="no join screen, no database, no gate — the old single-user "
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
