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
  --sat:62%; --lum:38%; --chip-lum:94%; --chip-text:28%;
  --tap:44px;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0f1115; --surface:#171a20;
    --fg:#e7e9ee; --mut:#98a0ad; --line:#262a32;
    --accent:#7c93ff; --accent-fg:#0f1115;
    --sat:48%; --lum:70%; --chip-lum:22%; --chip-text:78%;
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
.brand{display:flex;align-items:baseline;gap:8px;margin-bottom:10px}
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
.shead h2{margin:0;font-size:15px;font-weight:500;color:var(--mut);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sect{margin:0;padding:20px 16px 2px;font-size:13px;font-weight:600;
  letter-spacing:.05em;text-transform:uppercase;color:var(--mut)}
.rows{padding:6px 8px 0}
.row{
  display:flex;align-items:center;gap:12px;width:100%;
  min-height:var(--tap);padding:11px 12px;border-radius:11px;
  text-align:left;text-decoration:none;color:inherit;font-size:16px;
}
.row:active{background:var(--surface)}
.row .tick{width:3px;align-self:stretch;border-radius:2px;background:hsl(var(--h) var(--sat) var(--lum));flex:none}
.row .name{flex:1;min-width:0}
.row .name b{display:block;font-weight:600}
.row .name small{display:block;font-size:13px;color:var(--mut)}
.row .meta{font-size:13px;color:var(--mut);flex:none}
.row .code{flex:none}
.row a.name{text-decoration:none;color:inherit}
.blank{padding:64px 24px;text-align:center;color:var(--mut)}

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
.mine{margin:10px 16px 0;padding:16px;border-radius:12px;background:var(--surface)}
.mine .score{font-size:26px;font-weight:700;letter-spacing:-.02em}
.mine p{margin:5px 0 0;font-size:13px;color:var(--mut)}
.tally{display:flex;gap:22px;margin-top:14px}
.tally div{font-size:13px;color:var(--mut)}
.tally b{display:block;font-size:20px;font-weight:650;color:var(--fg);
  font-variant-numeric:tabular-nums}

.rtop{display:flex;align-items:center;gap:6px}
.back{display:flex;align-items:center;gap:5px;height:var(--tap);padding:0 10px 0 4px;
  margin-left:-4px;font-size:16px;color:var(--accent);font-weight:500}
.rtop .code{margin-left:auto}
/* Four sizes only -- 26/20/16/13, roughly a 1.25 step. h3 separates itself by
   weight and colour rather than a fifth size that would read as body text. */
article{padding:22px 18px 118px;max-width:70ch;margin:0 auto}
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
.dock button.primary{background:var(--accent);color:var(--accent-fg)}
#ask{position:absolute;z-index:9;display:none;padding:9px 15px;border-radius:10px;
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
#sheet{position:fixed;inset:0;z-index:11;display:none;background:rgba(0,0,0,.45)}
#sheet.on{display:block}
#sheet .card{position:absolute;left:0;right:0;bottom:0;background:var(--bg);
  border-radius:16px 16px 0 0;padding:18px 16px calc(18px + env(safe-area-inset-bottom));
  max-height:88dvh;overflow-y:auto}
#sheet h3{margin:0 0 14px;font-size:20px}
#sheet label{display:block;font-size:13px;color:var(--mut);margin:14px 0 6px}
#sheet select,#sheet .opt{width:100%;min-height:var(--tap);font-size:16px;border-radius:11px;
  border:1px solid var(--line);background:var(--surface);color:var(--fg);padding:0 12px}
#sheet .opt{display:flex;align-items:center;gap:11px;margin-top:9px;text-align:left}
#sheet .opt b{font-weight:600}
#sheet .opt span{color:var(--mut);font-size:13px}
#rec{margin-top:14px;padding:16px;border-radius:12px;background:var(--surface);text-align:center;display:none}
#rec.on{display:block}
#rec .time{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
#rec .dot{display:inline-block;width:11px;height:11px;border-radius:50%;background:#e5484d;
  margin-right:8px;animation:pulse 1.4s infinite}
@keyframes pulse{50%{opacity:.25}}
@media (prefers-reduced-motion:reduce){#rec .dot{animation:none}}
#prog{margin-top:16px;display:none}
#prog.on{display:block}
#prog .bar{height:10px;border-radius:5px;background:var(--surface);overflow:hidden}
#prog .fill{height:100%;width:0;background:var(--accent);transition:width .18s linear}
#prog .txt{margin-top:9px;font-size:13px;color:var(--mut);text-align:center}
#prog.err .fill{background:#e5484d}
.job .bar{height:4px;border-radius:2px;background:var(--line);margin-top:6px;overflow:hidden}
.job .bar i{display:block;height:100%;background:var(--accent);width:0;transition:width .3s}
.job .col{flex:1;min-width:0}
#logbtn{width:100%;margin-top:10px;min-height:var(--tap);border-radius:11px;
  background:var(--surface);font-size:13px;color:var(--mut)}
#logbox{display:none;margin-top:8px;padding:12px;border-radius:11px;background:var(--surface);
  font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre;
  overflow-x:auto;max-height:44vh;overflow-y:auto}
#logbox.on{display:block}
#jobs{padding:0 16px}
.job{display:flex;align-items:center;gap:11px;padding:11px 12px;margin-top:8px;
  border-radius:11px;background:var(--surface);font-size:16px}
.job .st{font-size:13px;color:var(--mut);margin-left:auto;text-align:right}
.job.failed{border:1px solid #e5484d}
.job.failed .st{color:#e5484d}
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
}
@media print{
  .top,.dock,#list,.rtop,#fab,#busy,#ask,#quiz{display:none!important}
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
  <div style="padding:0 16px">
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
</section>

<button id="fab" aria-label="Add a lecture or notes">+</button>

<div id="busy" role="status" aria-live="polite"></div>

<div id="sheet">
  <div class="card">
    <h3>Add to the library</h3>
    <label for="subj">Subject</label>
    <select id="subj"></select>
    <button class="opt" id="opt-rec"><b>Record this class</b><span>keep the screen on</span></button>
    <button class="opt" id="opt-audio"><b>Upload a recording</b><span>m4a, mp3, mp4</span></button>
    <button class="opt" id="opt-doc"><b>Upload notes or slides</b><span>pdf, txt, md</span></button>
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
  <button id="share">Share</button>
  <button id="dl">Download</button>
  <button id="print">Print</button>
</div>

<script>
const DATA = __DATA__;
const nav = document.getElementById('nav'), body = document.getElementById('body');
const backBtn = document.getElementById('back');
const q = document.getElementById('q'), rcode = document.getElementById('rcode');
const brand = document.getElementById('brand'), shead = document.getElementById('shead');
const scode = document.getElementById('scode'), sname = document.getElementById('sname');
let current = null;                      // the note being read, or null
let view = {code: null, title: null};    // which level the hash puts us on

// Hue per department prefix. Colour says which subject you are in, so the code
// chip reads at a glance without parsing the number.
const HUES = {MC:245, CY:150, EE:38, ME:210, BS:175, HS:345, SA:275, NC:80};
const hue = code => HUES[code.slice(0, 2)] ?? 220;

// ---- Three levels: subjects -> one subject -> one note. -------------------
// Each level is a real URL and each step down is a pushState, so the Android
// back gesture and the browser back button both climb one level rather than
// leaving the page. Nothing keeps its own back stack: route() reads the hash,
// and the hash is the only thing that decides what is on screen.
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

const hashOf = (code, title) =>
  '#' + (code ? encodeURIComponent(code) : '')
      + (title ? '/' + encodeURIComponent(title) : '');

function go(code, title) {
  history.pushState(null, '', hashOf(code, title));
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
  b.onclick = () => go(s.code, n.title);
  return b;
}

// One vote per person per item -- the votes primary key says so, and this is
// only the switch. The count lives inside the control, so pressing it and
// seeing what it did are the same place.
function voteBtn(u) {
  const b = document.createElement('button');
  b.className = 'vote' + (u.voted ? ' on' : '');
  b.setAttribute('aria-pressed', u.voted ? 'true' : 'false');
  b.setAttribute('aria-label', (u.voted ? 'Remove your upvote from ' : 'Upvote ') + u.name);
  b.innerHTML = '▲ <span class="n"></span>';
  b.querySelector('.n').textContent = u.votes;
  b.onclick = async () => {
    b.disabled = true;
    try {
      const r = await fetch('/vote', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: u.id, on: !u.voted}),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || 'could not register that vote');
      // Refetch rather than patch: the vote changes the ranking too, and one
      // source of order beats two that can disagree.
      await refresh();
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

function block(label, items) {
  if (!items.length) return;
  const h = document.createElement('h2');
  h.className = 'sect';
  h.textContent = label;
  const rows = document.createElement('div');
  rows.className = 'rows';
  items.forEach(el => rows.appendChild(el));
  nav.append(h, rows);
}

function blank(text) {
  const p = document.createElement('p');
  p.className = 'blank';
  p.textContent = text;
  nav.appendChild(p);
}

// LEVEL 1: every subject, empty ones included. Nobody can add a chemistry
// recording to a subject the app never told them was there.
function renderSubjects() {
  // Only with a server behind it: a static export has no session and nothing
  // to count.
  if (live) {
    const b = document.createElement('button');
    b.className = 'row';
    b.style.setProperty('--h', 210);
    b.innerHTML = '<i class="tick"></i><span class="name"><b></b><small></small></span>';
    b.querySelector('b').textContent = 'Your contributions';
    b.querySelector('small').textContent = 'what you have added, and the votes it got';
    b.onclick = () => go('me', null);
    block('You', [b]);
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
    b.onclick = () => go(s.code, null);
    return b;
  }));
}

// LEVEL 2: one subject, grouped.
function renderSubject(s) {
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
    blank('Nothing in ' + s.code + ' yet. Tap + to record a class or add slides.');
  }
}

// LEVEL 2, sideways: what you personally have put in.
//
// Points are status and nothing else. Nothing in this app asks for a score
// before it shows you something, and no screen here has a lock on it -- that
// was the product decision, and this view is the whole of it.
async function renderMe() {
  const box = document.createElement('div');
  box.className = 'mine';
  nav.appendChild(box);
  waiting(box, 'Counting up what you have added…');
  let d;
  try {
    const r = await fetch('/me');
    if (!r.ok) throw new Error();
    d = await r.json();
  } catch (e) {
    box.textContent = 'Contributions need the server. Run: notes.py serve';
    return;
  }
  if (view.code !== 'me') return;    // they navigated on while this was in flight
  const p = d.points;
  box.innerHTML = '<div class="score"></div><p></p>'
    + '<div class="tally"><div><b class="u"></b>uploads</div>'
    + '<div><b class="r"></b>recordings</div><div><b class="v"></b>votes received</div></div>';
  box.querySelector('.score').textContent = p.score + (p.score === 1 ? ' point' : ' points');
  box.querySelector('p').textContent =
    'Points are a thank-you, not a key. Everything in the library is open to everyone.';
  box.querySelector('.u').textContent = p.uploads;
  box.querySelector('.r').textContent = p.recordings;
  box.querySelector('.v').textContent = p.votes_received;

  const line = (main, sub) => {
    const el = document.createElement('div');
    el.className = 'row';
    el.style.setProperty('--h', 210);
    el.innerHTML = '<i class="tick"></i><span class="name"><b></b><small></small></span>';
    el.querySelector('b').textContent = main;
    el.querySelector('small').textContent = sub;
    return el;
  };
  block('Notes & slides you added', d.uploads.map(u => line(
    u.name, u.subject + ' · ' + plural(u.votes, 'vote')
            + (u.status === 'visible' ? '' : ' · ' + u.status))));
  block('Classes you recorded', d.recordings.map(r => line(
    r.title, r.subject + ' · ' + (r.status === 'done' ? 'notes ready' : r.status))));
  if (!d.uploads.length && !d.recordings.length) {
    blank('Nothing from you yet. Tap + to record a class or add your slides.');
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
  if (!shown) blank('Nothing matches that. Try a subject code like CY1107.');
}

function render() {
  const needle = q.value.trim().toLowerCase();
  const s = view.code ? subjectOf(view.code) : null;
  nav.innerHTML = '';
  brand.hidden = !!s;
  shead.hidden = !s;
  if (s) {
    scode.textContent = s.code;
    scode.style.setProperty('--h', hue(s.code));
    sname.textContent = s.name;
  }
  if (needle) renderSearch(needle);
  else if (view.code === 'me') renderMe();
  else if (s) renderSubject(s);
  else renderSubjects();
}

// The router. One place decides which of the three levels you are looking at.
function route() {
  // Back out of practice first: the quiz is not a URL, so without this the
  // Android back gesture would leave the app from underneath an open quiz.
  if (quiz.hidden === false) quiz.hidden = true;
  const [code, title] =
    location.hash.slice(1).split('/').filter(Boolean).map(decodeURIComponent);
  // Your own contributions sit beside the subjects rather than inside one, so
  // back climbs out of it to level 1 like everything else. 'me' is not a
  // subject code and never will be; the twelve are fixed.
  if (code === 'me') {
    view = {code: 'me', title: null};
    closeRead();
    return render();
  }
  // Links shared before the three levels existed are just '#<note title>'.
  // Point them at the note and upgrade the URL in place.
  if (code && !title && !subjectOf(code)) {
    const owner = DATA.find(x => x.notes.some(n => n.title === code));
    if (owner) {
      history.replaceState(null, '', hashOf(owner.code, code));
      return route();
    }
  }
  const s = subjectOf(code);
  view = {code: s ? code : null, title: s && title ? title : null};
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

// LEVEL 3: the note itself. Called only by route(), which has already put the
// right URL in the bar, so this never touches history.
function openNote(n, s) {
  current = n;
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
}

function closeRead() {
  document.body.classList.remove('reading');
  practice.hidden = true;
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
document.getElementById('lback').onclick = () => history.back();
window.onpopstate = route;

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
  try {
    const r = await fetch('/data');
    if (!r.ok) return;
    const d = await r.json();
    DATA.length = 0; DATA.push(...d.subjects);
    if (!subj.options.length) {
      for (const c of d.codes) {
        const o = document.createElement('option');
        o.value = c.code; o.textContent = c.code + ' — ' + c.name;
        subj.appendChild(o);
      }
    }
    render();
    live = true;
  } catch { live = false; }
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

let lastDone = 0;
async function pollJobs() {
  if (!live) return;
  try {
    const r = await fetch('/jobs');
    const {jobs} = await r.json();
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

const openSheet = () => {
  // Adding a chemistry recording from the chemistry screen should not need
  // the dropdown at all.
  if (view.code) subj.value = view.code;
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
function upload(blob, name) {
  prog.classList.remove('err');
  prog.classList.add('on');
  fill.style.width = '0%';
  ptxt.textContent = 'Starting…';

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

  const fail = msg => {
    prog.classList.add('err');
    fill.style.width = '100%';
    ptxt.textContent = msg + ' — tap an option to try again';
  };

  xhr.onload = () => {
    let d = {};
    try { d = JSON.parse(xhr.responseText); } catch {}
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

document.getElementById('opt-audio').onclick = () => {
  fileInput.accept = 'audio/*,video/*'; fileInput.click();
};
document.getElementById('opt-doc').onclick = () => {
  fileInput.accept = '.pdf,.txt,.md'; fileInput.click();
};
fileInput.onchange = () => {
  const f = fileInput.files[0];
  if (f) upload(f, f.name);
  fileInput.value = '';
};

document.getElementById('opt-rec').onclick = async () => {
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
  if (!text || text.length < 12 || !current
      || !body.contains(sel.anchorNode) || panel.classList.contains('on')) {
    return hideAsk();
  }
  picked = text;
  const r = sel.getRangeAt(0).getBoundingClientRect();
  ask.style.top = (window.scrollY + r.top - 52) + 'px';
  ask.style.left = Math.max(12, Math.min(window.innerWidth - 130,
                                         r.left + r.width / 2 - 55)) + 'px';
  ask.classList.add('on');
});

ask.onclick = async () => {
  hideAsk();
  quote.textContent = picked;
  waiting(out, 'Reading that passage\u2026');
  panel.classList.add('on');
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
// than climb a level. Seed the levels above it before the first route.
const deep = location.hash.slice(1).split('/').filter(Boolean);
if (deep.length) {
  const here = location.hash;
  history.replaceState(null, '', '#');
  if (deep.length > 1) history.pushState(null, '', '#' + deep[0]);
  history.pushState(null, '', here);
}
route();
</script>
"""


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
    parsing markdown or costing an API call.
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
                          "questions": parse_questions(text)})
        for md in sorted((folder / "lectures").glob("*.md")):
            text = md.read_text()
            notes.append({"title": md.stem, "kind": "lecture", "md": text,
                          "questions": parse_questions(text)})
        if (folder / "uploads").is_dir():
            for f in sorted((folder / "uploads").glob("*")):
                uploads.append({"name": f.name, "path": os.path.relpath(f, relative_to)})
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
        threading.Thread(target=self._run, daemon=True).start()

    def add(self, path, subject, kind):
        # A document is already on disk by the time we get here, so it is done.
        # Queuing it would park a 2-second PDF behind an 11-minute lecture.
        done = kind == "document"
        with self.lock:
            self.seq += 1
            job = {"id": self.seq, "name": path.name, "subject": subject, "kind": kind,
                   "state": "done" if done else "queued",
                   "detail": f"filed under {subject}" if done else ""}
            self.items.insert(0, job)
            if not done:
                self.pending.append((job, path))
        if not done:
            self.wake.set()
        return job

    def snapshot(self):
        with self.lock:
            return list(self.items)

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
                    conn.execute("update lectures set status = 'done' "
                                 "where audio_key = %s", (str(path),))
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

import hashlib
import hmac
import http.cookies
import json
import secrets
import uuid

import psycopg

DB_URL = os.environ.get("RECARVE_DB_URL", "postgresql:///recarve_test")
SESSION_COOKIE = "recarve_session"
ENV_PATH = Path(__file__).resolve().parent / ".env"

# Requests are gated before they are dispatched, so a route added later is
# protected whether or not whoever adds it remembers auth exists. These are the
# only paths that opt out, and adding to this set is the deliberate act.
PUBLIC_PATHS = {"/join"}


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


def db_join(conn, code, name, roll_no):
    """Put a new person through join_with_invite. (id, status, is_admin) or None.

    None means the code was wrong, expired or used up -- and nothing is left
    behind, not the auth row and not the invite use.
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
        ok = conn.execute("select join_with_invite(%s, %s, %s)",
                          (code, name, roll_no)).fetchone()[0]
        if not ok:
            raise psycopg.Rollback(tx)
        # The first person in is the admin. Nobody can approve anybody
        # otherwise, so the invite that bootstraps the class also elects them.
        act_as(conn, None, local=True)
        if conn.execute("select count(*) from profiles").fetchone()[0] == 1:
            # trusted too: an untrusted uploader's files are forced pending by
            # the materials trigger, and the admin is the one person nobody
            # else can ever publish.
            conn.execute(
                "update profiles set status = 'approved', is_admin = true, "
                "trusted = true where id = %s",
                (user_id,),
            )
        row = conn.execute("select status, is_admin from profiles where id = %s",
                           (user_id,)).fetchone()
        result = (user_id, row[0], row[1])
    return result


def db_principal(conn, profile_id):
    """Who a session belongs to, or None. Read as themselves, so a deleted or
    never-created profile comes back empty rather than trusted."""
    try:
        row = conn.execute(
            "select name, status, is_admin from profiles where id = %s", (profile_id,)
        ).fetchone()
    except psycopg.errors.InvalidTextRepresentation:
        return None  # signed, but not by a version of us that minted uuids
    if not row:
        return None
    return {"id": str(profile_id), "name": row[0], "status": row[1], "admin": row[2]}


def db_pending(conn):
    return [
        {"id": str(r[0]), "name": r[1], "roll_no": r[2]}
        for r in conn.execute(
            "select id, name, roll_no from profiles where status = 'pending' "
            "order by created_at"
        )
    ]


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


def db_vote(conn, material_id, user_id, on):
    """Add or drop one person's vote, and return the item's new state.

    Nothing here checks whether they have voted already: the votes primary key
    does, and a second insert raises. That is the point -- one place decides,
    and it is the same place in production as in the tests.
    """
    if on:
        conn.execute("insert into votes (material_id, voter_id) values (%s, %s)",
                     (material_id, user_id))
    else:
        conn.execute("delete from votes where material_id = %s and voter_id = %s",
                     (material_id, user_id))
    row = conn.execute(
        "select count(*), bool_or(voter_id = %s) from votes where material_id = %s",
        (user_id, material_id),
    ).fetchone()
    return {"votes": row[0], "voted": bool(row[1])}


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


def db_backfill(conn, library):
    """Register whatever is already on disk, once, in the admin's name.

    Everything here predates the database and somebody has to own it. The admin
    is the only account certain to exist, and this runs as them rather than as
    the table owner, so the same policies apply as to any other upload.
    """
    row = conn.execute(
        "select id from profiles where is_admin and status = 'approved' "
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
:root{color-scheme:light dark;--bg:#fcfcfd;--fg:#14161b;--mut:#656b76;--line:#e1e4ea;--accent:#3355e8}
@media (prefers-color-scheme:dark){
  :root{--bg:#0f1115;--fg:#e7e9ee;--mut:#98a0ad;--line:#262a32;--accent:#7a92ff}}
*{box-sizing:border-box}
body{margin:0;min-height:100dvh;display:grid;place-items:center;padding:24px;background:var(--bg);
  color:var(--fg);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
main{width:100%;max-width:23rem}
h1{font-size:1.45rem;margin:0 0 .3rem;letter-spacing:-.01em}
p{color:var(--mut);margin:0 0 1.4rem}
label{display:block;font-size:.8rem;color:var(--mut);margin:0 0 .3rem}
input{width:100%;padding:.7rem .8rem;margin:0 0 .9rem;font-size:1rem;border:1px solid var(--line);
  border-radius:10px;background:transparent;color:var(--fg)}
input:focus{outline:2px solid var(--accent);outline-offset:-1px;border-color:transparent}
button{min-height:44px;width:100%;font:600 1rem/1 inherit;border:0;border-radius:10px;
  background:var(--accent);color:#fff}
button[disabled]{opacity:.5}
.err{color:#d1344b;font-size:.88rem;min-height:1.2em;margin:.7rem 0 0}
.row{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:.75rem 0;
  border-bottom:1px solid var(--line)}
.row button{width:auto;padding:0 .9rem}
.row small{display:block;color:var(--mut);font-size:.8rem}
</style>
<main>__BODY__</main>
"""

JOIN_BODY = r"""<h1>recarve</h1>
<p>Section I notes. You need the invite code from someone already in.</p>
<form id="f">
  <label for="nm">Name</label><input id="nm" required autocomplete="name">
  <label for="roll">Roll number</label><input id="roll" required autocomplete="off">
  <label for="code">Invite code</label><input id="code" required autocomplete="off" autocapitalize="off">
  <button>Join</button>
  <p class="err" id="err"></p>
</form>
<script>
const $ = i => document.getElementById(i);
$('f').onsubmit = async e => {
  e.preventDefault();
  $('err').textContent = '';
  try {
    const res = await fetch('/join', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: $('nm').value, roll_no: $('roll').value, code: $('code').value})});
    const j = await res.json().catch(() => ({}));
    if (res.ok) return location.reload();
    $('err').textContent = j.error || 'could not join';
  } catch (e) { $('err').textContent = 'no connection to the server'; }
};
</script>
"""

WAIT_BODY = r"""<h1>Almost in</h1>
<p>Your request is with the admin. This page lets you through the moment they
approve you — leave it open, it rechecks itself.</p>
<script>setTimeout(() => location.reload(), 15000);</script>
"""

BLOCKED_BODY = """<h1>No access</h1>
<p>This account has been blocked. Talk to whoever runs the class library.</p>
"""

ADMIN_BODY = r"""<h1>Pending</h1>
<p>Everyone waiting to be let in.</p>
<div id="list">loading…</div>
<script>
async function load() {
  const el = document.getElementById('list');
  const j = await (await fetch('/pending')).json();
  if (!j.pending.length) { el.textContent = 'Nobody waiting.'; return; }
  el.innerHTML = '';
  for (const p of j.pending) {
    const row = document.createElement('div');
    row.className = 'row';
    const who = document.createElement('div');
    who.innerHTML = '<b></b><small></small>';
    // textContent, not innerHTML: the name and roll number are whatever the
    // joiner typed, and this page is looked at by the one admin account.
    who.querySelector('b').textContent = p.name;
    who.querySelector('small').textContent = p.roll_no || '';
    const btn = document.createElement('button');
    btn.textContent = 'Approve';
    btn.onclick = async () => {
      btn.disabled = true;
      await fetch('/approve', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: p.id})});
      load();
    };
    row.append(who, btn);
    el.append(row);
  }
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

    secret = b"" if args.no_auth else session_secret()
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
                return True
            # The front page is the one thing an outsider may see, and only so
            # they can ask to be let in.
            if path in ("/", "/index.html"):
                body = JOIN_BODY if not self.me else (
                    BLOCKED_BODY if self.me["status"] == "blocked" else WAIT_BODY)
                self.send_html(GATE_PAGE.replace("__BODY__", body))
                return False
            self.reply(403, {"error": "you are not approved to read this yet"})
            return False

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
            if self.path == "/data":
                subjects = build_data(args.library, args.out.parent)
                # Disk says what exists; the database says who added it and how
                # the class voted. --no-auth has neither a database nor anyone
                # to credit, which is the whole point of --no-auth.
                if self.me:
                    with db(self.me["id"]) as conn:
                        apply_meta(subjects, *db_meta(conn, self.me["id"]))
                return self.reply(200, {"subjects": subjects,
                                        "codes": [{"code": c, "name": n.replace("-", " ")}
                                                  for c, (n, _) in SUBJECTS.items()]})
            if self.path == "/me":
                if not self.me:
                    return self.reply(404, {"error": "this server is running with --no-auth"})
                with db(self.me["id"]) as conn:
                    return self.reply(200, db_contributions(conn, self.me["id"]))
            if self.path == "/jobs":
                return self.reply(200, {"jobs": jobs.snapshot()})
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
                    return self.reply(200, {"pending": db_pending(conn)})
            return super().do_GET()

        def is_admin(self):
            return bool(self.me and self.me["admin"])

        def do_POST(self):
            if self.path == "/join":
                return self.do_join()
            if self.path == "/approve":
                return self.do_approve()
            if self.path == "/upload":
                return self.do_upload()
            if self.path == "/vote":
                return self.do_vote()
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
                code = (req.get("code") or "").strip()[:64]
                if not (name and roll and code):
                    return self.reply(
                        400, {"error": "name, roll number and invite code are all required"})
                with db() as conn:
                    got = db_join(conn, code, name, roll)
            except psycopg.errors.UniqueViolation:
                return self.reply(409, {"error": "that roll number has already joined"})
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
                if n > 500 * 1024 * 1024:
                    return self.reply(413, {"error": "file over 500MB"})

                raw = urllib.parse.unquote(self.headers.get("X-Filename", "upload"))
                # Never trust a client-supplied filename with a path in it.
                name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(raw).name)[:120] or "upload"
                subject = self.headers.get("X-Subject", "").strip()
                code = resolve_subject(subject) if subject else guess_subject(name)
                if not code:
                    return self.reply(400, {"error": f"pick a subject for {name}"})

                ext = Path(name).suffix.lower()
                is_audio = ext in AUDIO_EXTS
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
            """One vote per person per item. The primary key is the referee.

            An unauthenticated caller never reaches this: /vote is not in
            PUBLIC_PATHS, so parse_request has already refused them.
            """
            if not self.me:
                return self.reply(404, {"error": "this server is running with --no-auth"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n > 4000:
                    self.close_connection = True
                    return self.reply(413, {"error": "too much"})
                req = json.loads(self.rfile.read(n) or b"{}")
                target = (req.get("id") or "").strip()
                if not target:
                    return self.reply(400, {"error": "which item?"})
                with db(self.me["id"]) as conn:
                    state = db_vote(conn, target, self.me["id"], bool(req.get("on", True)))
            except psycopg.errors.UniqueViolation:
                return self.reply(409, {"error": "you have already voted for this"})
            except (psycopg.errors.InvalidTextRepresentation,
                    psycopg.errors.ForeignKeyViolation):
                return self.reply(404, {"error": "no such item"})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            return self.reply(200, state)

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
    title = f"{path.stem}" + (f" — {SUBJECTS[code][0].replace('-', ' ')}" if code else "")
    body = f"# {title}\n\n"

    if args.no_notes:
        body += f"## Transcript\n\n```\n{transcript}\n```\n"
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
        body += f"{notes}\n\n---\n\n<details><summary>Full transcript</summary>\n\n```\n{transcript}\n```\n\n</details>\n"

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
    sv.add_argument("--no-auth", action="store_true",
                    help="no join screen, no database, no gate — the old single-user "
                         "behaviour, for working on this laptop")
    sv.add_argument("--verbose", action="store_true")
    sv.set_defaults(func=serve)

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
