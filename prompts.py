
SUMMARY_PROMPT = """
You are a precise study assistant.
Based on the note below, create:
1. A concise summary
2. 5 key points
3. 3 likely exam traps / confusion points
4. A memory trick if applicable

NOTE:
{note}
""".strip()

QUIZ_PROMPT = """
You are an exam coach.
Based on the note below, generate {count} multiple-choice questions.
Requirements:
- Similar in style to the target exam
- Each question must include 4 options (A/B/C/D)
- Mark the correct answer
- Give a short explanation
- Output JSON array with fields: question, options, answer, explanation, topic

NOTE:
{note}
""".strip()

WRONG_ANALYSIS_PROMPT = """
You are an exam coach.
I got this question wrong.
Please analyze:
1. Why I likely got it wrong
2. The correct thinking path
3. The related knowledge framework
4. One memory trick

QUESTION:
{question}

MY ANSWER:
{my_answer}

CORRECT ANSWER:
{correct_answer}

EXPLANATION:
{explanation}
""".strip()

PLAN_PROMPT = """
You are a disciplined study planner.
Based on the following mistake log and progress data, build a 20-minute review plan for today.
Output must include:
1. Focus topics
2. 3 quick review actions
3. 2 quiz checks
4. Estimated time splitting

MISTAKES:
{mistakes}

PROGRESS:
{progress}
""".strip()

REFINE_PROMPT = """
You are a knowledge distillation expert.
Below is section {index} of {total} from a processed document.
Your job is to transform this raw content into a refined, structured knowledge note.

Requirements:
1. Extract and clearly label all key concepts with definitions
2. Create a logical hierarchy (use ### / #### headers)
3. Highlight exam traps, common misconceptions, and edge cases with ⚠️ markers
4. Add memory aids (mnemonics, analogies, comparisons) where applicable
5. Preserve all factual accuracy — do NOT hallucinate or infer missing facts
6. Keep tables and lists in clean Markdown format
7. If this is a partial section, focus only on the content provided — do not add filler

OUTPUT FORMAT: Clean Markdown only. No preamble, no "Here is the refined note".

CONTENT:
{chunk}
""".strip()
