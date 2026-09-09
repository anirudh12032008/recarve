#!/usr/bin/env python3
"""Lecture audio -> transcript + study notes. Runs locally on Apple Silicon.

    ./notes.py lecture.m4a

Transcription is local (mlx-whisper, free). Notes use the Claude API.
"""

import argparse
import os
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
.rows{padding:6px 8px 0}
.row{
  display:flex;align-items:center;gap:12px;width:100%;
  min-height:var(--tap);padding:11px 12px;border-radius:11px;
  text-align:left;text-decoration:none;color:inherit;font-size:16px;
}
.row:active{background:var(--surface)}
.row .tick{width:3px;align-self:stretch;border-radius:2px;background:hsl(var(--h) var(--sat) var(--lum));flex:none}
.row .name{flex:1;min-width:0}
.row .meta{font-size:13px;color:var(--mut);flex:none}
.blank{padding:64px 24px;text-align:center;color:var(--mut)}

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
body.reading #fab{display:none}
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
.dock button:active{opacity:.75}
body:not(.reading) .dock{display:none}

@media (min-width:760px){
  body{display:flex}
  #list{width:320px;flex:none;border-right:1px solid var(--line);height:100dvh;overflow-y:auto;position:sticky;top:0}
  #read{flex:1;display:block;min-width:0}
  body.reading #list{display:block}
  .back{display:none}
  article{padding:30px 40px 110px}
  .dock{left:320px}
  body:not(.reading) .dock{display:flex}
}
@media print{
  .top,.dock,#list,.rtop{display:none!important}
  #read{display:block!important}
  article{padding:0;max-width:none}
  details{background:none;border:1px solid #999}
}
</style>

<section id="list">
  <div class="top">
    <div class="brand"><b>recarve</b><span>Section I</span></div>
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
    <button class="back" id="back" aria-label="Back to all notes">&lsaquo; Notes</button>
    <span class="code" id="rcode"></span>
  </div>
  <article id="body"><p class="blank">Pick a lecture to start reading.</p></article>
</section>

<button id="fab" aria-label="Add a lecture or notes">+</button>

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

<div class="dock">
  <button id="share" class="primary">Share</button>
  <button id="dl">Download</button>
  <button id="print">Print</button>
</div>

<script>
const DATA = __DATA__;
const nav = document.getElementById('nav'), body = document.getElementById('body');
const q = document.getElementById('q'), rcode = document.getElementById('rcode');
let current = null;

// Hue per department prefix. Colour says which subject you are in, so the code
// chip reads at a glance without parsing the number.
const HUES = {MC:245, CY:150, EE:38, ME:210, BS:175, HS:345, SA:275, NC:80};
const hue = code => HUES[code.slice(0, 2)] ?? 220;

function render(filter) {
  const needle = (filter || '').trim().toLowerCase();
  nav.innerHTML = '';
  let shown = 0;

  for (const s of DATA) {
    const subjHit = !needle || (s.code + ' ' + s.name).toLowerCase().includes(needle);
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
    nav.appendChild(g);

    const rows = document.createElement('div');
    rows.className = 'rows';
    rows.style.setProperty('--h', hue(s.code));

    for (const n of notes) {
      const b = document.createElement('button');
      b.className = 'row';
      b.innerHTML = '<i class="tick"></i><span class="name"></span>';
      b.querySelector('.name').textContent = n.title;
      b.onclick = () => open_(n, s);
      rows.appendChild(b);
    }
    for (const u of files) {
      const a = document.createElement('a');
      a.className = 'row'; a.href = u.path; a.target = '_blank'; a.rel = 'noopener';
      a.innerHTML = '<i class="tick"></i><span class="name"></span><span class="meta">file</span>';
      a.querySelector('.name').textContent = u.name;
      rows.appendChild(a);
    }
    nav.appendChild(rows);
  }

  if (!shown) {
    const p = document.createElement('p');
    p.className = 'blank';
    p.textContent = 'Nothing matches that. Try a subject code like CY1107.';
    nav.appendChild(p);
  }
}

function open_(n, s) {
  current = n;
  rcode.textContent = s.code;
  rcode.style.setProperty('--h', hue(s.code));
  body.innerHTML = marked.parse(n.md);

  // Wide tables scroll inside their own box instead of stretching the page.
  body.querySelectorAll('table').forEach(t => {
    const box = document.createElement('div');
    box.className = 'scroll-x';
    t.replaceWith(box); box.appendChild(t);
  });

  if (window.renderMathInElement) {
    renderMathInElement(body, {
      delimiters: [
        {left:'$$', right:'$$', display:true},
        {left:'$', right:'$', display:false},
        {left:'\\(', right:'\\)', display:false},
        {left:'\\[', right:'\\]', display:true},
      ],
      throwOnError: false,
    });
  }

  document.body.classList.add('reading');
  window.scrollTo(0, 0);
  // A real history entry, so the Android back gesture and the browser back
  // button return to the list instead of leaving the page.
  history.pushState({note: n.title}, '', '#' + encodeURIComponent(n.title));
}

function close_() {
  document.body.classList.remove('reading');
  current = null;
}

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

document.getElementById('back').onclick = () => history.back();
window.onpopstate = close_;

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
    render(q.value);
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
    const active = jobs.filter(j => j.state !== 'done' || Date.now() - lastDone < 8000);
    jobsBox.innerHTML = '';
    jobs.slice(0, 4).forEach(j => jobsBox.appendChild(jobRow(j)));
    const done = jobs.filter(j => j.state === 'done').length;
    if (done !== lastDone) { lastDone = done; refresh(); }
  } catch {}
}

const openSheet = () => { sheet.classList.add('on'); };
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
  closeSheet();
  const r = await fetch('/revise', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({subject: subj.value}),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) return alert(d.error || 'Could not build a revision sheet');
  refresh();
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
  out.textContent = 'Thinking...';
  panel.classList.add('on');
  try {
    const res = await fetch('/explain', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: picked, title: current ? current.title : ''}),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.status);
    out.innerHTML = marked.parse(data.text);
    if (window.renderMathInElement) {
      renderMathInElement(out, {delimiters: [
        {left:'$$', right:'$$', display:true}, {left:'$', right:'$', display:false},
      ], throwOnError: false});
    }
  } catch (err) {
    // The static export has no server, so say that rather than "failed".
    out.textContent = String(err).includes('JSON') || String(err).includes('Failed to fetch')
      ? 'Explain needs the server. Run: notes.py serve'
      : 'Could not explain that: ' + err.message;
  }
};

document.getElementById('close').onclick = () => panel.classList.remove('on');

q.oninput = () => render(q.value);
render('');

// Deep link: opening #<title> goes straight to that note.
const want = decodeURIComponent(location.hash.slice(1));
if (want) {
  for (const s of DATA) {
    const n = s.notes.find(x => x.title === want);
    if (n) { open_(n, s); break; }
  }
}
</script>
"""


def build_data(library, relative_to):
    """The whole library as plain data: one entry per subject that has content.

    Shared by `export` (baked into the page) and the server's /data endpoint
    (fetched live), so the browser sees the same shape either way.
    """
    lib = Path(library)
    data = []
    for code, (name, _) in SUBJECTS.items():
        folder = lib / f"{code}-{name}"
        notes, uploads = [], []
        rev = folder / "revision.md"
        if rev.is_file():
            notes.append({"title": "Revision sheet", "md": rev.read_text()})
        for md in sorted((folder / "lectures").glob("*.md")):
            notes.append({"title": md.stem, "md": md.read_text()})
        if (folder / "uploads").is_dir():
            for f in sorted((folder / "uploads").glob("*")):
                uploads.append({"name": f.name, "path": os.path.relpath(f, relative_to)})
        if notes or uploads:
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

    if not data:
        raise SystemExit("library is empty — nothing to export")

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
        try:
            path.unlink(missing_ok=True)  # transcript is cached; audio is the bulk
            log(f"removed {path.name} from the inbox", "cleanup", 1)
        except OSError:
            pass
        self._set(job, "done", "notes ready")


EXPLAIN_PROMPT = """A student is reading their lecture notes and highlighted a passage they do not
understand. Explain just that passage.

Rules:
- 3-5 sentences. This is a quick doubt, not a lecture.
- Plain language first, then the technical statement.
- If it is a formula, say what each symbol means and when you would use it.
- Maths as LaTeX: $...$ inline, $$...$$ display.
- Answer only what was highlighted. Do not summarise the whole note.
- If the passage is too fragmentary to explain, say so and ask what specifically is unclear."""


def serve(args):
    """Serve the library and answer 'explain this' taps from the phone.

    The API key never leaves the Mac: the phone posts the highlighted text here
    and gets prose back.
    """
    import hashlib
    import json
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    root = Path(__file__).resolve().parent
    export(args)  # always serve the current library

    global LOG_PATH
    Path(args.library).mkdir(parents=True, exist_ok=True)
    LOG_PATH = Path(args.library) / "recarve.log"
    cache_dir = Path(args.library) / ".explains"
    cache_dir.mkdir(parents=True, exist_ok=True)
    inbox = Path(args.library) / ".inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    budget = {"left": args.max_explains}
    jobs = Jobs(args)

    # The queue lives in memory but the audio lives on disk, so a restart used
    # to strand an upload forever. Anything still in the inbox gets re-queued;
    # transcription resumes from its checkpoint rather than starting over.
    for leftover in sorted(inbox.glob("*")):
        if leftover.suffix.lower() in AUDIO_EXTS:
            code = leftover.name.split("-", 1)[0]
            if code in SUBJECTS:
                jobs.add(leftover, code, "audio")
                log(f"re-queued {leftover.name} from a previous run", "resume")

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, fmt, *a):
            if args.verbose:
                super().log_message(fmt, *a)

        def translate_path(self, path):
            if path.split("?")[0] in ("/", "/index.html"):
                return str(args.out)
            return super().translate_path(path)

        def do_GET(self):
            if self.path == "/data":
                return self.reply(200, {"subjects": build_data(args.library, args.out.parent),
                                        "codes": [{"code": c, "name": n.replace("-", " ")}
                                                  for c, (n, _) in SUBJECTS.items()]})
            if self.path == "/jobs":
                return self.reply(200, {"jobs": jobs.snapshot()})
            if self.path == "/log":
                try:
                    tail = LOG_PATH.read_text().splitlines()[-200:]
                except OSError:
                    tail = []
                return self.reply(200, {"lines": tail})
            return super().do_GET()

        def do_POST(self):
            if self.path == "/upload":
                return self.do_upload()
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

        def do_upload(self):
            """Raw body upload: filename and subject ride in headers.

            Deliberately not multipart -- the cgi module is gone in Python 3.13+
            and a hand-rolled parser is a bug farm for zero benefit here.
            """
            import json as _json
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
                job = jobs.add(dest, code, "audio" if is_audio else "document")
                return self.reply(200, {"job": job})
            except SystemExit as e:
                return self.reply(400, {"error": str(e)})
            except Exception as e:
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})

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

        def reply(self, code, obj):
            body = json.dumps(obj).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # phone hung up; nothing useful left to do

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("no ANTHROPIC_API_KEY set - Explain will return an error", file=sys.stderr)

    srv = ThreadingHTTPServer((args.host, args.port), partial(Handler, directory=str(root)))
    log(f"http://{lan_ip()}:{args.port}   <- open this on your phone", "ready")
    log(f"library {args.library}", "ready")
    log(f"log file {LOG_PATH}", "ready")
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
    sv.add_argument("--host", default="0.0.0.0", help="0.0.0.0 exposes it to your wifi")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--out", type=Path, default=Path(__file__).parent / "site" / "index.html")
    sv.add_argument("--notes-model", default="claude-haiku-4-5")
    sv.add_argument("--max-cost", type=float, default=1.00)
    sv.add_argument("--max-explains", type=int, default=300,
                    help="spend guard: stop answering after this many taps")
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
