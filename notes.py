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
    "CY1110": ("Environmental-Science", ["envsci", "env-sci", "environmental", "cy1110"]),
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


def subject_dir(library, code, kind):
    """library/CY1107-Engineering-Chemistry/lectures|uploads/"""
    d = Path(library) / f"{code}-{SUBJECTS[code][0]}" / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


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


def transcribe(audio_path, model, language, verbose=True, checkpoint=None):
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

        result = mlx_whisper.transcribe(
            window,
            path_or_hf_repo=repo,
            language=language,  # None => detect this chunk on its own
            initial_prompt=HINGLISH_PROMPT,
            condition_on_previous_text=False,  # stops one bad chunk poisoning the rest
            verbose=None,
        )

        chunk_segs = result.get("segments") or []
        if not chunk_segs:
            pos += step
            continue

        # Drop the trailing segment unless this is the final chunk; it was cut off.
        keep = chunk_segs if is_last or len(chunk_segs) == 1 else chunk_segs[:-1]
        offset = pos / SAMPLE_RATE
        for seg in keep:
            segments.append((offset + seg["start"], seg["text"].strip()))
        languages.append(result.get("language", "?"))

        if verbose:
            mins = int(offset // 60)
            print(
                f"  [{mins:>3}m] {result.get('language', '?')}  {keep[-1]['text'].strip()[:70]}",
                file=sys.stderr,
            )

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


def make_notes(transcript, notes_lang, model):
    import anthropic

    client = anthropic.Anthropic()
    system = NOTES_PROMPT.format(notes_lang=NOTES_LANG[notes_lang])
    kwargs = dict(
        model=model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": transcript}],
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


PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>recarve — Section I</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/15.0.7/marked.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.11/katex.min.css">
<script defer src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.11/katex.min.js"></script>
<script defer src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.11/contrib/auto-render.min.js"></script>
<style>
:root{--bg:#fff;--fg:#16181d;--mut:#666e7a;--line:#e3e6ea;--accent:#2f6df6;--card:#f7f8fa}
@media(prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e8eaed;--mut:#9aa3ad;--line:#2a2e35;--accent:#7aa2ff;--card:#1b1e24}}
*{box-sizing:border-box}
body{margin:0;font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--fg);display:flex;min-height:100vh}
aside{width:270px;flex:none;border-right:1px solid var(--line);padding:18px 14px;overflow-y:auto;height:100vh;position:sticky;top:0}
h1{font-size:15px;margin:0 0 14px;letter-spacing:.02em}
#q{width:100%;padding:8px 10px;margin-bottom:14px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font-size:14px}
.subj{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);margin:16px 0 6px}
a.item{display:block;padding:6px 9px;border-radius:7px;color:var(--fg);text-decoration:none;font-size:13.5px;cursor:pointer}
a.item:hover{background:var(--card)}
a.item.on{background:var(--accent);color:#fff}
main{flex:1;padding:34px 44px;max-width:860px;overflow-x:auto}
main img{max-width:100%}
pre{background:var(--card);padding:12px;border-radius:8px;overflow-x:auto;font-size:12.5px}
table{border-collapse:collapse}td,th{border:1px solid var(--line);padding:5px 9px}
details{margin:6px 0;padding:8px 12px;background:var(--card);border-radius:8px}
summary{cursor:pointer;color:var(--accent)}
.empty{color:var(--mut)}
@media(max-width:720px){body{flex-direction:column}aside{width:100%;height:auto;position:static;border-right:0;border-bottom:1px solid var(--line)}main{padding:20px}}
</style>
<aside>
  <h1>recarve · Section I</h1>
  <input id="q" placeholder="Search all notes…" autocomplete="off">
  <nav id="nav"></nav>
</aside>
<main id="body"><p class="empty">Pick a lecture on the left.</p></main>
<script>
const DATA = __DATA__;
const nav = document.getElementById('nav'), body = document.getElementById('body'), q = document.getElementById('q');
let current = null;

function render(filter) {
  nav.innerHTML = '';
  const needle = (filter || '').toLowerCase();
  for (const s of DATA) {
    // A hit on the subject itself ("chemistry", "CY1107") shows everything under it.
    const subjHit = !needle || (s.code + ' ' + s.name).toLowerCase().includes(needle);
    const items = s.notes.filter(n =>
      subjHit || n.title.toLowerCase().includes(needle) || n.md.toLowerCase().includes(needle));
    const files = s.uploads.filter(u => subjHit || u.name.toLowerCase().includes(needle));
    if (!items.length && !files.length) continue;
    const h = document.createElement('div');
    h.className = 'subj'; h.textContent = s.code + ' · ' + s.name;
    nav.appendChild(h);
    for (const n of items) {
      const a = document.createElement('a');
      a.className = 'item' + (current === n ? ' on' : ''); a.textContent = n.title;
      a.onclick = () => { current = n; show(n); render(q.value); };
      nav.appendChild(a);
    }
    for (const u of files) {
      const a = document.createElement('a');
      a.className = 'item'; a.textContent = '📎 ' + u.name; a.href = u.path; a.target = '_blank';
      nav.appendChild(a);
    }
  }
  if (!nav.children.length) nav.innerHTML = '<p class="empty">No matches.</p>';
}
function show(n) {
  body.innerHTML = marked.parse(n.md);
  // Notes are full of LaTeX; markdown alone renders it as literal $$ noise.
  if (window.renderMathInElement) {
    renderMathInElement(body, {
      delimiters: [
        {left: '$$', right: '$$', display: true},
        {left: '$', right: '$', display: false},
        {left: '\\\\(', right: '\\\\)', display: false},
        {left: '\\\\[', right: '\\\\]', display: true},
      ],
      throwOnError: false,  // a malformed formula shows as red text, not a blank page
    });
  }
  body.scrollIntoView();
}
q.oninput = () => render(q.value);
render('');
</script>
"""


def export(args):
    """Write the whole library to one self-contained HTML page."""
    import json

    lib = Path(args.library)
    if not lib.exists():
        raise SystemExit(f"no library at {lib} yet — transcribe or add something first")

    out = args.out
    data = []
    for code, (name, _) in SUBJECTS.items():
        folder = lib / f"{code}-{name}"
        notes, uploads = [], []
        for md in sorted((folder / "lectures").glob("*.md")):
            notes.append({"title": md.stem, "md": md.read_text()})
        for f in sorted((folder / "uploads").glob("*")) if (folder / "uploads").exists() else []:
            uploads.append({"name": f.name, "path": os.path.relpath(f, out.parent)})
        if notes or uploads:
            data.append({"code": code, "name": name.replace("-", " "), "notes": notes, "uploads": uploads})

    if not data:
        raise SystemExit("library is empty — nothing to export")

    out.parent.mkdir(parents=True, exist_ok=True)
    # </script> inside note text would close the tag early.
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    out.write_text(PAGE.replace("__DATA__", payload))
    lectures = sum(len(s["notes"]) for s in data)
    files = sum(len(s["uploads"]) for s in data)
    print(f"{lectures} lectures, {files} uploads, {len(data)} subjects -> {out}", file=sys.stderr)


def list_subjects(args):
    for code, (name, _) in SUBJECTS.items():
        folder = Path(args.library) / f"{code}-{name}"
        lectures = len(list((folder / "lectures").glob("*.md"))) if folder.exists() else 0
        uploads = len(list((folder / "uploads").iterdir())) if (folder / "uploads").exists() else 0
        print(f"  {code}  {name:<34} {lectures:>3} lectures  {uploads:>3} uploads")


def process(path, args):
    print(f"\n{path.name}", file=sys.stderr)
    outdir, code = destination(path, args, "lectures")
    out = outdir / f"{path.stem}.md"

    # Never pay for the same lecture twice.
    if out.exists() and not (args.force or args.redo_notes):
        print(f"  already done: {out} (--force to redo)", file=sys.stderr)
        return

    # Transcribing is free but slow; caching it means a failed or re-run notes
    # step never costs another 11 minutes of Whisper.
    cache = Path(args.library) / ".transcripts" / f"{path.stem}.txt"
    partial = cache.with_suffix(".partial.json")
    if cache.exists() and not args.force:
        transcript = cache.read_text()
        print(f"  reusing cached transcript ({len(transcript.splitlines())} lines)", file=sys.stderr)
    else:
        if args.force and partial.exists():
            partial.unlink()
        t0 = time.time()
        segments, languages = transcribe(path, args.model, args.lang, checkpoint=partial)
        if not segments:
            print("  no speech found, skipping", file=sys.stderr)
            return
        kept = drop_repeats(segments)
        transcript = format_transcript(kept)
        mins = segments[-1][0] / 60
        dropped = len(segments) - len(kept)
        note = f", dropped {dropped} repeated" if dropped else ""
        print(
            f"  transcribed {mins:.0f} min in {time.time() - t0:.0f}s "
            f"(detected: {', '.join(sorted(set(languages)))}{note})",
            file=sys.stderr,
        )
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
        notes, usage = make_notes(transcript, args.notes_lang, args.notes_model)
        cost = usage.input_tokens / 1e6 * rate_in + usage.output_tokens / 1e6 * rate_out
        print(
            f"  notes: {usage.input_tokens} in / {usage.output_tokens} out "
            f"(~${cost:.3f} on {args.notes_model})",
            file=sys.stderr,
        )
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
