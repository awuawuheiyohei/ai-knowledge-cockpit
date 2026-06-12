#!/usr/bin/env python3
"""
AI Knowledge Cockpit — CLI entry point.

Study tools: summary, quiz, wrong-question tracking, daily plans.

Put your Markdown notes in notes/ and use the commands below.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from string import Template

from dotenv import load_dotenv

from prompts import (
    SUMMARY_PROMPT, QUIZ_PROMPT, WRONG_ANALYSIS_PROMPT, PLAN_PROMPT, REFINE_PROMPT,
)
from llm_client import LLMClient
from knowledge import (
    chunk_text, list_notes, load_input_file, load_note, save_note, save_note_file,
    save_output, save_json,
)
from review import add_wrong_question, load_wrong_questions, load_progress

# Load .env on startup
load_dotenv()

# Template-based prompts (safe from format-string injection)
_SUMMARY_T = Template(SUMMARY_PROMPT)
_QUIZ_T = Template(QUIZ_PROMPT)
_WRONG_T = Template(WRONG_ANALYSIS_PROMPT)
_PLAN_T = Template(PLAN_PROMPT)
_REFINE_T = Template(REFINE_PROMPT)


# ── helpers ────────────────────────────────────────────────────────

def _extract_json(text):
    """Extract JSON array from LLM response, with robust fallbacks."""
    import re as _re

    text = text.strip()
    # Strip common fence styles
    text = _re.sub(r'^```(?:json)?\s*\n?', '', text)
    text = _re.sub(r'\n?```\s*$', '', text)
    text = _re.sub(r'^~~~\s*\n?', '', text)
    text = _re.sub(r'\n?~~~\s*$', '', text)

    match = _re.search(r'\[.*\]', text, _re.DOTALL)
    if not match:
        raise ValueError(
            f'No JSON array found in LLM response. First 200 chars: {text[:200]}'
        )
    try:
        return json.loads(match.group())
    except json.JSONDecodeError as e:
        raise ValueError(f'Invalid JSON in LLM response: {e}\nRaw: {match.group()[:300]}')


# ── study commands ──────────────────────────────────────────────────

def cmd_list_notes(_args):
    notes = list_notes()
    if not notes:
        print('No notes found in notes/. Add markdown files first.')
        return
    for n in notes:
        print(n)


def cmd_summary(args):
    note = load_note(args.note)
    client = LLMClient()
    result = client.chat(_SUMMARY_T.substitute(note=note))
    p = save_output('latest_summary.md', result)
    print(f'Saved summary to {p}')
    print(result)


def cmd_quiz(args):
    note = load_note(args.note)
    client = LLMClient()
    raw = client.chat(_QUIZ_T.substitute(note=note, count=args.count), temperature=0.4)
    questions = _extract_json(raw)
    p = save_json('latest_quiz.json', questions)
    print(f'Saved quiz to {p}')
    print(json.dumps(questions, ensure_ascii=False, indent=2))


def cmd_add_wrong(args):
    record = {
        'question': args.question,
        'my_answer': args.my_answer,
        'correct_answer': args.correct_answer,
        'explanation': args.explanation,
        'topic': args.topic,
    }
    p = add_wrong_question(record)
    print(f'Saved wrong question to {p}')


def cmd_analyze_wrong(args):
    client = LLMClient()

    if args.id is not None:
        # Look up by index in wrong_questions.json
        wrongs = load_wrong_questions()
        if args.id < 0 or args.id >= len(wrongs):
            print(f'Error: id {args.id} out of range (0–{len(wrongs)-1})', file=sys.stderr)
            sys.exit(1)
        record = wrongs[args.id]
        question = record['question']
        my_answer = record['my_answer']
        correct_answer = record['correct_answer']
        explanation = record['explanation']
    else:
        question = args.question
        my_answer = args.my_answer
        correct_answer = args.correct_answer
        explanation = args.explanation

    result = client.chat(_WRONG_T.substitute(
        question=question,
        my_answer=my_answer,
        correct_answer=correct_answer,
        explanation=explanation,
    ))
    p = save_output('latest_wrong_analysis.md', result)
    print(f'Saved analysis to {p}')
    print(result)


def cmd_refine_note(args):
    """Refine raw text into structured notes via REFINE_PROMPT (chunked LLM calls)."""
    raw = load_input_file(args.input)
    chunks = chunk_text(raw, args.chunk_size)
    if not chunks:
        print('Error: input file is empty', file=sys.stderr)
        sys.exit(1)

    client = LLMClient()
    refined_parts = []
    total = len(chunks)

    for i, chunk in enumerate(chunks, 1):
        print(f'Refining section {i}/{total}...', file=sys.stderr)
        result = client.chat(
            _REFINE_T.substitute(index=i, total=total, chunk=chunk),
            temperature=0.2,
        )
        refined_parts.append(f'<!-- Refined section {i}/{total} -->\n\n{result.strip()}')

    topic = args.topic or Path(args.input).stem
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    source = str(Path(args.input).resolve())
    body = '\n\n'.join(refined_parts)
    content = (
        f'---\n'
        f'source: {source}\n'
        f'refined_at: {now}\n'
        f'chunks: {total}\n'
        f'---\n\n'
        f'# {topic}\n\n'
        f'{body}\n'
    )

    if args.subject:
        p = save_note(args.subject, topic, content)
    else:
        filename = topic if topic.endswith('.md') else f'{topic}.md'
        p = save_note_file(filename, content)

    print(f'Saved refined note to {p} ({total} chunk(s))')


def cmd_plan(_args):
    mistakes = load_wrong_questions()
    progress = load_progress()
    client = LLMClient()
    result = client.chat(_PLAN_T.substitute(
        mistakes=json.dumps(mistakes, ensure_ascii=False, indent=2),
        progress=json.dumps(progress, ensure_ascii=False, indent=2),
    ))
    p = save_output('today_plan.md', result)
    print(f'Saved plan to {p}')
    print(result)


# ── argument parser ────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(description='AI Knowledge Cockpit')
    sub = parser.add_subparsers(dest='command', required=True)

    sp = sub.add_parser('list-notes')
    sp.set_defaults(func=cmd_list_notes)

    sp = sub.add_parser('summary')
    sp.add_argument('--note', required=True, help='Path to markdown note')
    sp.set_defaults(func=cmd_summary)

    sp = sub.add_parser('quiz')
    sp.add_argument('--note', required=True, help='Path to markdown note')
    sp.add_argument('--count', type=int, default=5)
    sp.set_defaults(func=cmd_quiz)

    sp = sub.add_parser('add-wrong')
    sp.add_argument('--question', required=True)
    sp.add_argument('--my-answer', required=True)
    sp.add_argument('--correct-answer', required=True)
    sp.add_argument('--explanation', required=True)
    sp.add_argument('--topic', default='general')
    sp.set_defaults(func=cmd_add_wrong)

    sp = sub.add_parser('analyze-wrong')
    sp.add_argument('--id', type=int, default=None, help='Index in wrong_questions.json')
    sp.add_argument('--question', default=None)
    sp.add_argument('--my-answer', default=None)
    sp.add_argument('--correct-answer', default=None)
    sp.add_argument('--explanation', default=None)
    sp.set_defaults(func=cmd_analyze_wrong)

    sp = sub.add_parser('plan')
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser(
        'refine-note',
        help='Refine raw text into structured notes/ Markdown via LLM',
    )
    sp.add_argument('--input', required=True, help='Path to raw text/markdown file')
    sp.add_argument('--topic', default=None, help='Note title (defaults to input filename)')
    sp.add_argument(
        '--subject',
        default=None,
        help='Optional subfolder under notes/ (e.g. cissp). Omit to save flat in notes/',
    )
    sp.add_argument(
        '--chunk-size',
        type=int,
        default=6000,
        help='Max characters per LLM chunk (default: 6000)',
    )
    sp.set_defaults(func=cmd_refine_note)

    return parser


if __name__ == '__main__':
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
